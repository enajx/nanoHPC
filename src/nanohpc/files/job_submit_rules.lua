-- Shared rules. The tables above this line are generated per cluster:
-- submit_limit, default_partition, job_types (batch | interactive | any), walls (minutes), gpu_limits,
-- interactive_machine_limits (minutes), interactive_machines (shared nodes by batch partition).
local jobs_snapshot = nil
local unconfirmed_submissions = {}

-- Count distinct array tasks, stopping as soon as the submission is too large.
local function requested_tasks(indices)
    if not indices then return 1 end
    local seen, count = {}, 0
    for part in indices:gsub("%%.*$", ""):gmatch("[^,]+") do
        local first, last, step = part:match("^(%d+)%-(%d+):(%d+)$")
        if not first then first, last = part:match("^(%d+)%-(%d+)$") end
        if not first then first = part:match("^(%d+)$"); last = first end
        first, last, step = tonumber(first), tonumber(last), tonumber(step) or 1
        if not first or not last or step < 1 then return 0 end
        for task = first, last, step do
            if not seen[task] then seen[task] = true; count = count + 1 end
            if count > submit_limit then return count end
        end
    end
    return count
end

-- Count GPU requests in the standard Slurm TRES syntax, including typed GPUs.
local function gpu_count(value)
    local count = 0
    for resource in (value or ""):gmatch("[^,]+") do
        if resource:match("^gres/gpu") then
            count = count + (tonumber(resource:match(":(%d+)$")) or 0)
        end
    end
    return count
end

-- Split a Slurm hostlist at commas outside brackets (gpu[3-4,6],cpu1).
local function hostlist_tokens(nodes)
    local tokens, depth, start = {}, 0, 1
    nodes = nodes or ""
    for index = 1, #nodes do
        local char = nodes:sub(index, index)
        if char == "[" then depth = depth + 1 end
        if char == "]" then depth = depth - 1 end
        if char == "," and depth == 0 then
            if index > start then table.insert(tokens, nodes:sub(start, index - 1)) end
            start = index + 1
        end
    end
    if start <= #nodes then table.insert(tokens, nodes:sub(start)) end
    return tokens
end

-- True when one hostlist item names a machine, including a numeric range.
local function token_names_machine(token, machine)
    if token == machine then return true end
    local prefix, numbers, suffix = token:match("^(.-)%[([^%]]+)%](.*)$")
    if not prefix or machine:sub(1, #prefix) ~= prefix then return false end
    if suffix ~= "" and machine:sub(-#suffix) ~= suffix then return false end
    local middle = machine:sub(#prefix + 1, #machine - #suffix)
    if not middle:match("^%d+$") then return false end
    for part in numbers:gmatch("[^,]+") do
        if middle == part then return true end
        local first, last = part:match("^(%d+)%-(%d+)$")
        if first and (#first == #middle or first:sub(1, 1) ~= "0")
            and tonumber(middle) >= tonumber(first) and tonumber(middle) <= tonumber(last) then return true end
    end
    return false
end

-- True when a Slurm hostlist names this machine.
local function lists_machine(nodes, machine)
    for _, token in ipairs(hostlist_tokens(nodes)) do
        if token_names_machine(token, machine) then return true end
    end
    return false
end

-- Keep long batch jobs off machines shared with an interactive partition.
local function keep_off_interactive_machines(job_desc, partition, time_limit)
    local limit = interactive_machine_limits[partition]
    if not limit or time_limit <= limit then return slurm.SUCCESS end
    local machines = interactive_machines[partition]
    for _, machine in ipairs(machines) do
        if lists_machine(job_desc.req_nodes, machine) then
            slurm.log_user("Rejected: %s jobs may run at most %g hours on %s, kept free for interactive shells. Request a shorter time or leave out --nodelist.", partition, limit / 60, table.concat(machines, ", "))
            return slurm.ERROR
        end
    end
    local excluded = {}
    for _, name in ipairs(hostlist_tokens(job_desc.exc_nodes)) do table.insert(excluded, name) end
    for _, machine in ipairs(machines) do
        if not lists_machine(job_desc.exc_nodes, machine) then table.insert(excluded, machine) end
    end
    job_desc.exc_nodes = table.concat(excluded, ",")
    return slurm.SUCCESS
end

-- The interactive shell is one documented Bash login shell, not a background command.
local function is_interactive_shell(job_desc)
    return job_desc.script == nil
        and job_desc.argc == 2
        and job_desc.argv ~= nil
        and job_desc.argv[0] == "bash"
        and job_desc.argv[1] == "-l"
end

-- Reject invalid requests before they enter the queue, without external calls.
function slurm_job_submit(job_desc, part_list, submit_uid)
    -- Slurm's Lua job table can omit submissions made within the same second.
    -- Track those calls only for explanatory messages, never to reject jobs.
    -- Native accounting remains the atomic authority for the submission cap.
    if jobs_snapshot ~= slurm.jobs then
        jobs_snapshot = slurm.jobs
        unconfirmed_submissions = {}
    end
    local active = 0
    for _, job in pairs(slurm.jobs) do
        local state = job.job_state % 256
        if job.user_id == job_desc.user_id and (state == 0 or state == 1 or state == 2) then
            active = active + (job.array_task_cnt or 1)
        end
    end
    if active + requested_tasks(job_desc.array_inx) > submit_limit then
        slurm.log_user("Rejected: limit is %d running and pending jobs per user across all partitions. Array tasks count separately. Wait for jobs to finish or cancel your own pending jobs.", submit_limit)
        return slurm.ERROR
    end
    for partition in (job_desc.partition or default_partition):gmatch("[^,]+") do
        local job_type = job_types[partition]
        if job_type == "batch" and job_desc.script == nil then
            slurm.log_user("Rejected: %s accepts submitted background jobs only. Use sbatch or cluster-submit, or a partition that accepts the interactive shell.", partition)
            return slurm.ERROR
        end
        if job_type == "interactive" and not is_interactive_shell(job_desc) then
            slurm.log_user("Rejected: %s accepts only the interactive shell: srun --partition=%s --pty bash -l. Submit background jobs with sbatch to a batch partition.", partition, partition)
            return slurm.ERROR
        end
        local wall = walls[partition]
        if wall and job_desc.time_limit ~= slurm.NO_VAL and job_desc.time_limit > wall then
            slurm.log_user("Rejected: %s allows at most %d minutes per job. Request a shorter time or choose another partition.", partition, wall)
            return slurm.ERROR
        end
        local time_limit = job_desc.time_limit
        if time_limit == nil or time_limit == slurm.NO_VAL then time_limit = wall end
        if keep_off_interactive_machines(job_desc, partition, time_limit) ~= slurm.SUCCESS then
            return slurm.ERROR
        end
        local requested = math.max(gpu_count(job_desc.tres_per_job), gpu_count(job_desc.tres_per_node), gpu_count(job_desc.tres_per_socket), gpu_count(job_desc.tres_per_task))
        if gpu_limits[partition] and requested > gpu_limits[partition] then
            slurm.log_user("Rejected: %s allows at most %d GPUs for one user at a time. Request fewer GPUs or choose another partition.", partition, gpu_limits[partition])
            return slurm.ERROR
        end
    end
    local pending = (unconfirmed_submissions[job_desc.user_id] or 0) + requested_tasks(job_desc.array_inx)
    unconfirmed_submissions[job_desc.user_id] = pending
    if active + pending >= submit_limit then
        slurm.log_user("Submission limit: at most %d running and pending jobs per user across all partitions. Array tasks count separately. If this submission is refused, wait for jobs to finish or cancel your own pending jobs.", submit_limit)
    end
    return slurm.SUCCESS
end

-- Only administrators can move a job to another partition: the job type, time, and GPU rules
-- are checked at submission, so a user must cancel and resubmit instead.
function slurm_job_modify(job_desc, job_rec, part_list, modify_uid)
    if modify_uid ~= 0 and job_desc.partition ~= nil and job_desc.partition ~= job_rec.partition then
        slurm.log_user("Rejected: a job cannot change partition after submission. Cancel and resubmit it to the intended partition.")
        return slurm.ERROR
    end
    -- A user's update cannot remove this exclusion, including by lengthening the job's time limit.
    if modify_uid ~= 0 and (job_desc.exc_nodes ~= nil or job_desc.req_nodes ~= nil or job_desc.time_limit ~= nil) then
        local time_limit = job_desc.time_limit
        if time_limit == nil or time_limit == slurm.NO_VAL then time_limit = job_rec.time_limit end
        if job_desc.exc_nodes == nil then job_desc.exc_nodes = job_rec.exc_nodes end
        if job_desc.req_nodes == nil then job_desc.req_nodes = job_rec.req_nodes end
        for partition in job_rec.partition:gmatch("[^,]+") do
            local effective_time = time_limit
            if effective_time == nil or effective_time == slurm.NO_VAL then effective_time = walls[partition] end
            if keep_off_interactive_machines(job_desc, partition, effective_time) ~= slurm.SUCCESS then
                return slurm.ERROR
            end
        end
    end
    return slurm.SUCCESS
end

return slurm.SUCCESS

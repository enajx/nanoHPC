"""The wizard's seven steps, in order. Each step shows part of cluster.yml and writes every change to the file
in memory at once; the file on disk changes only on save."""

from nanohpc.wizard.common import Step
from nanohpc.wizard.forms import ExtrasStep, PartitionsStep, WebsiteStep
from nanohpc.wizard.machines import MachinesStep, StorageStep
from nanohpc.wizard.review import ReviewStep
from nanohpc.wizard.users import UsersStep

STEP_CLASSES: tuple[type[Step], ...] = (
    MachinesStep,
    StorageStep,
    UsersStep,
    PartitionsStep,
    WebsiteStep,
    ExtrasStep,
    ReviewStep,
)

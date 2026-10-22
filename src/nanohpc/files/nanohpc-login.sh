# Show nanohpc-hello (the nanoHPC art and the user's own summary) in interactive logins on the front node.
# It runs only in a terminal, is capped at 10 seconds (Ctrl+C skips it), and the shell always continues.
if [ -t 1 ] && command -v nanohpc-hello >/dev/null 2>&1; then
  timeout --foreground 10 nanohpc-hello
fi

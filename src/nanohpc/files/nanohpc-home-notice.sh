# Show the current user's private home usage box on an interactive front-node login.
if [ -t 1 ]; then
  _nanohpc_login_user=$(id -un)
  case $_nanohpc_login_user in
    ''|*[!A-Za-z0-9_-]*) ;;
    *)
      _nanohpc_home_notice=/var/lib/nanohpc-home-notices/$_nanohpc_login_user
      if [ -r "$_nanohpc_home_notice" ]; then
        cat "$_nanohpc_home_notice"
      fi
      ;;
  esac
  unset _nanohpc_login_user _nanohpc_home_notice
fi

{ writeShellApplication, writeShellScript, writeTextDir, coder, postgresql_16, systemd, coreutils, curl, awscli2, coder-aws, tailscale, python3 }:
let
  postgres = postgresql_16;
  startServer = writeShellScript "coder-trial-start-server" ''
    set -eu
    export CODER_SESSION_DURATION="$(${python3}/bin/python3 ${../scripts/coder_workspace/settings.py} session_duration)"
    sessionHours="''${CODER_SESSION_DURATION%h}"
    export CODER_DEFAULT_OAUTH_REFRESH_LIFETIME="$((sessionHours + 24))h"
    export CODER_DISABLE_SESSION_EXPIRY_REFRESH=false
    exec ${coder}/bin/coder --global-config "$HOME/.local/state/coder-trial/server" server --cache-dir "$HOME/.cache/coder-trial"
  '';
  awsCredentials = writeShellScript "coder-aws-credentials" ''
    region="$(${python3}/bin/python3 ${../scripts/coder_workspace/settings.py} region)"
    if [ -f "$HOME/.aws/coder-workspaces.credentials" ]; then
      exec ${coreutils}/bin/env -u AWS_PROFILE AWS_CONFIG_FILE=/dev/null \
        AWS_SHARED_CREDENTIALS_FILE="$HOME/.aws/coder-workspaces.credentials" \
        ${awscli2}/bin/aws --region "$region" configure export-credentials --profile coder-workspaces --format process
    fi
    exec ${coreutils}/bin/env -u AWS_PROFILE -u AWS_CONFIG_FILE \
      ${awscli2}/bin/aws --region "$region" configure export-credentials --profile default --format process
  '';
  awsConfig = writeTextDir "config" ''
    [profile coder-workspaces]
    credential_process = ${awsCredentials}
  '';
  initDatabase = writeShellScript "coder-trial-init-db" ''
    set -eu
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
      ${postgres}/bin/initdb -D "$PGDATA" --no-locale --encoding=UTF8 \
        --auth-local=peer --auth-host=reject
    fi
  '';
  ensureDatabase = writeShellScript "coder-trial-ensure-db" ''
    set -eu
    for _ in $(${coreutils}/bin/seq 1 60); do
      if ${postgres}/bin/pg_isready -q; then
        if [ "$(${postgres}/bin/psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'coder'")" != 1 ]; then
          ${postgres}/bin/createdb coder
        fi
        exit 0
      fi
      ${coreutils}/bin/sleep 0.5
    done
    echo 'Coder trial database did not become ready' >&2
    exit 1
  '';
  databaseUnit = writeTextDir "lib/systemd/user/coder-trial-postgres.service" ''
    [Unit]
    Description=PostgreSQL for the local Coder trial
    PartOf=coder-trial.service

    [Service]
    Type=exec
    Environment=PGDATA=%h/.local/state/coder-trial/postgres
    Environment=PGHOST=%t/coder-trial-postgres PGPORT=54329 PGDATABASE=postgres
    StateDirectory=coder-trial/postgres
    StateDirectoryMode=0700
    RuntimeDirectory=coder-trial-postgres
    RuntimeDirectoryMode=0700
    ExecStartPre=${initDatabase}
    ExecStart=${postgres}/bin/postgres -D %h/.local/state/coder-trial/postgres -k %t/coder-trial-postgres -p 54329 -c listen_addresses= -c unix_socket_permissions=0700 -c shared_buffers=32MB -c max_connections=20
    ExecStartPost=${ensureDatabase}
    TimeoutStartSec=60
    KillSignal=SIGINT
    Restart=on-failure
    RestartSec=3
    UMask=0077
    NoNewPrivileges=true
    MemoryHigh=192M
    MemoryMax=256M
  '';
  serverUnit = writeTextDir "lib/systemd/user/coder-trial.service" ''
    [Unit]
    Description=Local Coder dashboard trial
    Requires=coder-trial-postgres.service
    After=coder-trial-postgres.service network-online.target
    Wants=coder-reconcile.timer

    [Service]
    Type=exec
    StateDirectory=coder-trial/server
    StateDirectoryMode=0700
    CacheDirectory=coder-trial
    Environment=CODER_HTTP_ADDRESS=127.0.0.1:7080
    Environment=CODER_ACCESS_URL=http://127.0.0.1:7080
    Environment="CODER_PG_CONNECTION_URL=user=%u dbname=coder host=%t/coder-trial-postgres port=54329 sslmode=disable"
    Environment=CODER_TELEMETRY_ENABLE=false
    Environment=CODER_UPDATE_CHECK=false
    Environment=CODER_PROVISIONER_DAEMONS=1
    Environment=AWS_CONFIG_FILE=${awsConfig}/config AWS_PROFILE=coder-workspaces AWS_SDK_LOAD_CONFIG=1
    ExecStart=${startServer}
    Restart=on-failure
    RestartSec=3
    TimeoutStopSec=30
    KillSignal=SIGINT
    UMask=0077
    NoNewPrivileges=true
    MemoryHigh=768M
    MemoryMax=1536M
  '';
  reconcileUnit = writeTextDir "lib/systemd/user/coder-reconcile.service" ''
    [Unit]
    Description=Connect Coder dashboard-started AWS workers
    After=coder-trial.service
    PartOf=coder-trial.service
    [Service]
    Type=oneshot
    ExecStart=${coder-aws}/bin/coder-aws reconcile
    TimeoutStartSec=420
    UMask=0077
    MemoryMax=256M
  '';
  reconcileTimer = writeTextDir "lib/systemd/user/coder-reconcile.timer" ''
    [Unit]
    Description=Reconcile private Coder workspace bridges
    PartOf=coder-trial.service
    [Timer]
    OnActiveSec=10
    OnUnitInactiveSec=10
    Unit=coder-reconcile.service
  '';
in
writeShellApplication {
  name = "coder-trial";
  runtimeInputs = [ systemd coreutils curl tailscale ];
  text = ''
    case "''${1:-status}" in
      start)
        systemctl --user link --force \
          ${databaseUnit}/lib/systemd/user/coder-trial-postgres.service \
          ${serverUnit}/lib/systemd/user/coder-trial.service \
          ${reconcileUnit}/lib/systemd/user/coder-reconcile.service \
          ${reconcileTimer}/lib/systemd/user/coder-reconcile.timer
        systemctl --user daemon-reload
        systemctl --user start coder-trial.service
        for _ in $(seq 1 60); do
          if curl --fail --silent --output /dev/null http://127.0.0.1:7080/healthz; then
            echo 'Coder trial ready: http://127.0.0.1:7080/'
            exit 0
          fi
          if ! systemctl --user is-active --quiet coder-trial.service; then
            break
          fi
          sleep 1
        done
        echo 'Coder is not ready; inspect: journalctl --user -u coder-trial -u coder-trial-postgres' >&2
        exit 1
        ;;
      stop)
        systemctl --user stop coder-trial.service coder-trial-postgres.service coder-reconcile.timer
        ;;
      status)
        systemctl --user status coder-trial.service coder-trial-postgres.service --no-pager
        ;;
      logs)
        journalctl --user -u coder-trial.service -u coder-trial-postgres.service -n 80 --no-pager
        ;;
      login)
        exec ${coder}/bin/coder login http://127.0.0.1:7080
        ;;
      tailnet)
        # Coexists with T3 on 443. Serve is tailnet-only; this does not enable Funnel.
        sudo -n ${tailscale}/bin/tailscale serve --bg --https=8443 http://127.0.0.1:7080
        ${tailscale}/bin/tailscale serve status
        ;;
      *)
        echo 'Usage: coder-trial [start|stop|status|logs|login|tailnet]' >&2
        exit 2
        ;;
    esac
  '';
  meta.description = "On-demand local Coder dashboard trial with a private PostgreSQL database";
}

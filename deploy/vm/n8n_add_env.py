"""Prepare re-creating the n8n container with the MATTR AI bridge settings.

Docker can't add environment variables to a running container, so n8n is
started again with exactly its current settings plus three new ones. This
script only PREPARES that. It reads `docker inspect` and writes two files:

  ~/n8n.env     every environment variable n8n has now, plus the new ones (chmod 600)
  ~/n8n-run.sh  the `docker run` command, which reads the variables from ~/n8n.env

It changes nothing itself. Usage on the VM:

  python3 n8n_add_env.py [container-name]      (default: n8n)

It asks for the bridge secret without showing it on screen.
"""

import getpass
import json
import os
import shlex
import subprocess
import sys

NEW = {"NODE_FUNCTION_ALLOW_BUILTIN": "crypto,zlib", "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false"}
SECRET = "MATTR_BRIDGE_SECRET"


def inspect(*args) -> dict:
    return json.loads(subprocess.check_output(["docker", *args]))[0]


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "n8n"
    c = inspect("inspect", name)
    image = inspect("image", "inspect", c["Image"])
    h, cfg = c["HostConfig"], c["Config"]

    image_env = set(image["Config"].get("Env") or [])
    env = {}
    for line in cfg.get("Env") or []:
        if line in image_env:
            continue  # set by the image itself, not by your command
        k, _, v = line.partition("=")
        env[k] = v
    for k, v in NEW.items():
        if k in env and env[k] != v:
            print(f"note: {k} was {env[k]!r}, now {v!r}")
        env[k] = v
    if k_old := env.get("NODE_FUNCTION_ALLOW_BUILTIN_OLD"):
        print(f"note: NODE_FUNCTION_ALLOW_BUILTIN_OLD={k_old}")
    if SECRET in env:
        print(f"{SECRET} is already set; enter it again to replace it, or the same value.")
    secret = (os.environ.get(SECRET) or getpass.getpass(
        f"Paste the bridge secret ({SECRET}) and press Enter (nothing is shown): ")).strip()
    if len(secret) < 32:
        sys.exit("That looks too short. Make it with: openssl rand -hex 32")
    env[SECRET] = secret
    if any("\n" in v for v in env.values()):
        sys.exit("A variable contains a line break; this script can't carry it over. Ask for help.")

    args = ["docker", "run", "-d", "--name", c["Name"].lstrip("/")]
    rp = h.get("RestartPolicy") or {}
    if rp.get("Name") and rp["Name"] != "no":
        args += ["--restart", rp["Name"] + (f":{rp['MaximumRetryCount']}" if rp["Name"] == "on-failure"
                                           and rp.get("MaximumRetryCount") else "")]
    mode = h.get("NetworkMode") or "default"
    if mode not in ("default", "bridge"):
        args += ["--network", mode]
    for cport, binds in (h.get("PortBindings") or {}).items():
        port, _, proto = cport.partition("/")
        for b in binds or []:
            hip = b.get("HostIp") or ""
            args += ["-p", f"{hip + ':' if hip else ''}{b.get('HostPort') or ''}:{port}" + ("/udp" if proto == "udp" else "")]
    for m in c.get("Mounts") or []:
        src = m["Source"] if m["Type"] == "bind" else m.get("Name")
        if m["Type"] in ("bind", "volume") and src:
            args += ["-v", f"{src}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro")]
    if cfg.get("User") and cfg["User"] != (image["Config"].get("User") or ""):
        args += ["--user", cfg["User"]]
    for host in h.get("ExtraHosts") or []:
        args += ["--add-host", host]
    if h.get("ShmSize") and h["ShmSize"] != 67108864:
        args += ["--shm-size", str(h["ShmSize"])]
    if h.get("Memory"):
        args += ["--memory", str(h["Memory"])]
    for opt, val in (h.get("LogConfig") or {}).get("Config", {}).items():
        args += ["--log-opt", f"{opt}={val}"]
    if (h.get("LogConfig") or {}).get("Type") not in (None, "", "json-file"):
        args += ["--log-driver", h["LogConfig"]["Type"]]
    args += ["--env-file", os.path.expanduser("~/n8n.env")]
    # The exact image n8n runs now (its id), so re-creating never upgrades n8n by surprise.
    args.append(c["Image"])
    if cfg.get("Cmd") and cfg["Cmd"] != image["Config"].get("Cmd"):
        args += cfg["Cmd"]

    warn = [k for k in ("Privileged", "CapAdd", "Devices", "Dns", "SecurityOpt") if h.get(k)]
    extra_nets = [n for n in (c.get("NetworkSettings", {}).get("Networks") or {}) if n != mode]
    if cfg.get("Entrypoint") != image["Config"].get("Entrypoint"):
        warn.append("Entrypoint")

    env_path = os.path.expanduser("~/n8n.env")
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())
    run_path = os.path.expanduser("~/n8n-run.sh")
    with open(run_path, "w") as f:
        lines, cur = [], []
        for a in args[2:]:
            if a.startswith("-") and cur:
                lines.append(" ".join(cur))
                cur = []
            cur.append(shlex.quote(a))
        lines.append(" ".join(cur))
        f.write("#!/bin/sh\nset -e\ndocker run \\\n  " + " \\\n  ".join(lines) + "\n")
        for n in extra_nets:
            f.write(f"docker network connect {shlex.quote(n)} {shlex.quote(c['Name'].lstrip('/'))}\n")
    os.chmod(run_path, 0o700)

    print(f"\nWrote {env_path} ({len(env)} variables, readable only by you) and {run_path}:\n")
    print(open(run_path).read())
    print("Variables carried over:", ", ".join(k for k in env if k not in NEW and k != SECRET) or "(none)")
    print("Added:", ", ".join([*NEW, SECRET]))
    if warn:
        print("\nWARNING: n8n also uses", ", ".join(warn), "which this script does not copy. Ask before running.")


if __name__ == "__main__":
    main()

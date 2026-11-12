# ruff: noqa: RUF001
"""Fixed SSH bootstrap and restricted maintenance, with ephemeral password IPC."""

from __future__ import annotations

import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from threading import Event, Thread
from typing import Any

from harness.tunnel_platform import PlatformError, write_private
from harness.tunnels import endpoint, identity


class PasswordBroker:
    """ASKPASS obtains transient credentials through a private local socket."""

    def __init__(self, directory: Path, credentials: dict[str, str]) -> None:
        self.path = directory / "askpass.sock"
        self.credentials = credentials
        self.stop = Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen(2)
        self.server.settimeout(0.2)
        self.thread = Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        count = 0
        while not self.stop.is_set() and count < 8:
            try:
                peer, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with peer:
                peer.settimeout(2)
                prompt = peer.recv(1024).decode("utf-8", "replace").lower()
                value = self.credentials.get(
                    "key_passphrase" if "passphrase" in prompt else "password", ""
                )
                peer.sendall(value.encode())
            count += 1

    def __enter__(self) -> PasswordBroker:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.server.close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def askpass(socket_path: Path, prompt: str) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(3)
        peer.connect(str(socket_path))
        peer.sendall(prompt[:1024].encode())
        value = peer.recv(4096)
    sys.stdout.buffer.write(value)
    return 0


def askpass_main() -> int:
    return askpass(Path(os.environ["HARNESS_TUNNEL_ASKPASS"]), " ".join(sys.argv[1:]))


def ssh_command(target: dict[str, Any], known_hosts: Path, key: Path | None = None) -> list[str]:
    target = endpoint(target)
    executable = (
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe")
        if os.name == "nt"
        else "/usr/bin/ssh"
    )
    arguments = [
        executable,
        "-T",
        "-p",
        str(target["port"]),
        "-o",
        "ConnectTimeout=12",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
    ]
    if key is not None:
        arguments += ["-i", str(key)]
    return [*arguments, "--", f"{target['user']}@{target['host']}"]


def _initial_request(
    state_root: Path,
    target: dict[str, Any],
    credentials: dict[str, str],
    program: str,
    data: bytes,
) -> subprocess.CompletedProcess[bytes]:
    """Transient initial SSH access for fixed bootstrap/removal programs only."""
    directory = state_root / "ssh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = "/usr/bin/python3 -I -S -c " + shlex.quote(program)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=directory) as temporary:
        temporary_root = Path(temporary)
        key = None
        if credentials.get("private_key"):
            key = temporary_root / "identity"
            write_private(key, credentials["private_key"].encode())
        script = temporary_root / "askpass"
        # The installer runs this module from a root/admin-owned immutable runtime.
        script.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -S {shlex.quote(str(Path(__file__).with_name("tunnel_launcher.py")))} askpass "$@"\n'
        )
        script.chmod(0o700)
        target = endpoint(target)
        if target["user"] != "root":
            # Consume the credential line ourselves: NOPASSWD/cached sudo
            # does not read it and would otherwise prepend it to the tar.
            command = (
                "IFS= read -r harness_sudo_secret; "
                "if sudo -n true 2>/dev/null; then exec sudo -n " + command + "; "
                "else { printf '%s\\n' \"$harness_sudo_secret\"; cat; } | sudo -S -p '' "
                + command
                + "; fi"
            )
            data = (credentials.get("sudo_password", "") + "\n").encode() + data
        environment = {
            **os.environ,
            "SSH_ASKPASS": str(script),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": ":0",
            "HARNESS_TUNNEL_ASKPASS": str(temporary_root / "askpass.sock"),
        }
        if os.name == "nt":
            # Windows OpenSSH needs an executable ASKPASS helper, not a shell
            # script. The packaged launcher is installed as an executable by
            # the Windows installer; fail explicitly if it is unavailable.
            helper = state_root / "askpass.exe"
            if (
                credentials.get("password") or credentials.get("key_passphrase")
            ) and not helper.is_file():
                raise PlatformError("Не установлен Windows-компонент безопасного ввода SSH-пароля")
            environment["SSH_ASKPASS"] = str(helper)
        with PasswordBroker(temporary_root, credentials):
            try:
                result = subprocess.run(
                    [*ssh_command(target, directory / "known_hosts", key), command],
                    input=data,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=300,
                    check=False,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise PlatformError("Не удалось выполнить подготовку по SSH") from exc
    return result


def reconcile_remove(
    state_root: Path,
    profile_id: str,
    target: dict[str, Any],
    credentials: dict[str, str],
) -> None:
    """Fresh operator access confirms absence or removes only verified owned files."""
    result = _initial_request(
        state_root, target, credentials, maintenance_script(profile_id), b"remove\n"
    )
    if result.returncode or result.stdout.strip() != b'{"ok": true}':
        raise PlatformError("Удаление VPS не подтверждено; проверьте свежий SSH-доступ")


def bootstrap(
    state_root: Path,
    profile_id: str,
    target: dict[str, Any],
    credentials: dict[str, str],
    payload: dict[str, Any],
    engine: Path,
) -> dict[str, Any]:
    """Run only the owned bootstrap program; neither passwords nor config enter argv."""
    profile_id = identity(profile_id)
    directory = state_root / "ssh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    maintenance = directory / (profile_id + "-maintenance")
    if not maintenance.exists():
        keygen = (
            str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh-keygen.exe")
            if os.name == "nt"
            else "/usr/bin/ssh-keygen"
        )
        result = subprocess.run(
            [
                keygen,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "harness-tunnel",
                "-f",
                str(maintenance),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise PlatformError("Не удалось создать служебный ключ SSH")
    public_key = maintenance.with_suffix(".pub").read_text().strip()
    envelope = {**payload, "profile_id": profile_id, "maintenance_key": public_key}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = json.dumps(envelope).encode()
        info = tarfile.TarInfo("settings.json")
        info.size, info.mode = len(data), 0o600
        archive.addfile(info, io.BytesIO(data))
        archive.add(engine, arcname="sing-box", recursive=False)
    result = _initial_request(state_root, target, credentials, REMOTE_BOOTSTRAP, buffer.getvalue())
    if result.returncode or len(result.stdout) > 65536:
        raise PlatformError(
            "Подготовка VPS не завершена. Проверьте доступы, ОС и свободный порт 443"
        )
    try:
        reply = json.loads(result.stdout)
    except ValueError as exc:
        raise PlatformError("VPS вернул неподдерживаемый результат подготовки") from exc
    if (
        not isinstance(reply, dict)
        or set(reply) != {"ok", "certificate", "public_certificate", "maintenance_user"}
        or reply.get("ok") is not True
        or type(reply.get("public_certificate")) is not bool
        or reply.get("maintenance_user") not in {"root", "htun_" + profile_id[:12]}
        or not isinstance(reply.get("certificate"), str)
        or len(reply["certificate"]) > 16384
        or not reply["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    ):
        raise PlatformError("Подготовка VPS не завершена")
    return reply


def maintain(
    state_root: Path,
    profile_id: str,
    target: dict[str, Any],
    action: str,
    maintenance_user: str | None = None,
) -> None:
    if action not in {"status", "restart", "remove"}:
        raise PlatformError("Неизвестная операция обслуживания VPS")
    profile_id = identity(profile_id)
    if maintenance_user not in {None, "root", "htun_" + profile_id[:12]}:
        raise PlatformError("Некорректный пользователь обслуживания VPS")
    target = endpoint(target)
    directory = state_root / "ssh"
    try:
        for user in [maintenance_user] if maintenance_user else ["htun_" + profile_id[:12], "root"]:
            arguments = ssh_command(
                {**target, "user": user},
                directory / "known_hosts",
                directory / (profile_id + "-maintenance"),
            )
            # Maintenance never prompts or falls back to initial credentials.
            arguments[1:1] = ["-o", "BatchMode=yes"]
            result = subprocess.run(
                [*arguments, action],
                input=(action + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=45,
                check=False,
                env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"},
            )
            if result.returncode == 0:
                break
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlatformError("Служебный SSH-доступ недоступен") from exc
    if result.returncode:
        raise PlatformError("Операция обслуживания VPS не завершена")


# This bootstrap is fixed product code. Its input is an allowlisted archive,
# never an arbitrary operator command or raw imported server metadata.
REMOTE_BOOTSTRAP = r"""
import atexit,hashlib,io,json,os,pathlib,platform,re,shutil,signal,socket,subprocess,sys,tarfile,tempfile
backups={};temporary_paths=[];created_directories=[];started=[];restarted=[];firewall_added=[];finished=False
def mkdir_owned(path,mode=0o700):
 path=pathlib.Path(path)
 if path.is_symlink():raise RuntimeError('symlink collision')
 if path.exists():
  if not path.is_dir():raise RuntimeError('directory collision')
  return
 if not path.parent.exists():mkdir_owned(path.parent)
 path.mkdir(mode=mode);created_directories.append(path)
def delete_firewall(label):
 status=subprocess.check_output(['/usr/sbin/ufw','status','numbered']).decode()
 numbers=[int(match.group(1)) for line in status.splitlines() if label in line for match in [re.match(r'\[\s*(\d+)\]',line)] if match]
 for number in sorted(numbers,reverse=True):subprocess.run(['/usr/sbin/ufw','--force','delete',str(number)],check=True)
def publish(path,data,mode=0o600):
 path=pathlib.Path(path)
 if path.is_symlink(): raise RuntimeError('symlink collision')
 if path not in backups: backups[path]=(path.read_bytes(),path.stat().st_mode&0o777) if path.is_file() else None
 temporary=path.with_name('.'+path.name+'.harness-new')
 with temporary.open('xb') as target:
  temporary_paths.append(temporary)
  target.write(data if isinstance(data,bytes) else data.encode());target.flush();os.fsync(target.fileno())
 temporary.chmod(mode);os.replace(temporary,path)
def rollback():
 if finished:return
 for label in firewall_added:
  try:delete_firewall(label)
  except Exception:pass
 for name in reversed(started):
  subprocess.run(['systemctl','disable','--now',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for path,previous in reversed(list(backups.items())):
  try:
   if previous is None:path.unlink(missing_ok=True)
   else:path.write_bytes(previous[0]);path.chmod(previous[1])
  except OSError:pass
 for path in temporary_paths:
  try:path.unlink(missing_ok=True)
  except OSError:pass
 for directory in reversed(created_directories):
  try:directory.rmdir()
  except OSError:pass
 subprocess.run(['systemctl','daemon-reload'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for name in restarted:subprocess.run(['systemctl','restart',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
atexit.register(rollback)
for signum in (signal.SIGHUP,signal.SIGTERM):signal.signal(signum,lambda *_:sys.exit(1))
def run(args, data=None):
 p=subprocess.run(args,input=data,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=180)
 if p.returncode: raise RuntimeError('operation failed')
 return p.stdout
if os.geteuid()!=0: raise RuntimeError('root required')
if platform.machine().lower() not in ('x86_64','amd64'):raise RuntimeError('unsupported server architecture')
release=dict(line.rstrip().split('=',1) for line in pathlib.Path('/etc/os-release').read_text().splitlines() if '=' in line)
if release.get('ID','').strip('"') not in ('ubuntu','debian') or not pathlib.Path('/run/systemd/system').is_dir(): raise RuntimeError('unsupported platform')
data=sys.stdin.buffer.read(256*1024*1024+1)
if len(data)>256*1024*1024: raise RuntimeError('archive too large')
with tempfile.TemporaryDirectory(prefix='harness-tunnel-') as temp:
 root=pathlib.Path(temp)
 with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
  for member in archive.getmembers():
   if member.name not in ('settings.json','sing-box') or not member.isfile() or member.size>256*1024*1024: raise RuntimeError('bad archive')
   source=archive.extractfile(member)
   if source is None: raise RuntimeError('bad archive')
   with (root/member.name).open('xb') as target: shutil.copyfileobj(source,target)
 config=json.loads((root/'settings.json').read_bytes())
 profile=config['profile_id']
 import re
 if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',profile): raise RuntimeError('bad profile')
 target=pathlib.Path('/opt/harness-tunnel')/profile
 marker=target/'owner.json'
 if target.exists() and (not marker.is_file() or json.loads(marker.read_text()).get('profile_id')!=profile): raise RuntimeError('unowned collision')
 if hashlib.sha256((root/'sing-box').read_bytes()).hexdigest()!=config['engine_sha256']: raise RuntimeError('digest mismatch')
 if not target.exists():
  for family,ip in ((socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')):
   try:
    for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):
     probe=socket.socket(family,kind);probe.bind((ip,443));probe.close()
   except OSError as error:
    import errno
    if error.errno not in (errno.EAFNOSUPPORT,errno.EADDRNOTAVAIL):raise RuntimeError('port 443 busy')
 front=config.get('https_front',False)
 if front:
  packages=config['system_packages']
  if not isinstance(packages,dict) or not packages: raise RuntimeError('package audit required')
  simulation=run(['apt-get','-s','install','--no-install-recommends','haproxy','certbot'])
  for package,version in re.findall(rb'^Inst ([^ ]+)(?: \[[^]]+\])? \(([^ ]+)',simulation,re.M):
   if packages.get(package.decode())!=version.decode(): raise RuntimeError('unaudited package')
  run(['apt-get','install','-y','--no-install-recommends','haproxy','certbot'])
 mkdir_owned(target)
 publish(target/'sing-box',(root/'sing-box').read_bytes(),0o755)
 publish(target/'owner.json',json.dumps({'profile_id':profile}))
 etc=pathlib.Path('/etc/harness-tunnel')
 if etc.exists() and not (etc/'owner.json').is_file(): raise RuntimeError('unowned configuration')
 mkdir_owned(etc)
 if (etc/'owner.json').exists() and json.loads((etc/'owner.json').read_text()).get('profile_id')!=profile: raise RuntimeError('another tunnel owns port')
 publish(etc/'owner.json',json.dumps({'profile_id':profile}))
 def write(name,value):
  publish(etc/name,value)
 write('sing-box.json',json.dumps(config['server_config']))
 if front:write('haproxy.cfg',config['haproxy_config'])
 # A real IP certificate is preferred; the pinned self-signed fallback only
 # enables REALITY/Hysteria, never a supposedly browser-like Naive session.
 pair=run([str(target/'sing-box'),'generate','tls-keypair',config['certificate_server_name'],'--months','24']).decode()
 key=re.search(r'-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----',pair,re.S)
 certificate=re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',pair,re.S)
 if key is None or certificate is None:raise RuntimeError('certificate generation failed')
 write('key.pem',key.group(0)+'\n');write('cert.pem',certificate.group(0)+'\n')
 # HTTP-01 needs port80; leave an unrelated listener intact and retain the
 # explicitly reported certificate fallback if the challenge cannot start.
 public=False
 try:
  if not front:raise RuntimeError('optional HTTPS not enabled')
  result=run(['certbot','certonly','--standalone','--non-interactive','--agree-tos','--register-unsafely-without-email','--required-profile','shortlived','--cert-name','harness-'+profile,'--ip-address',config['ip']])
  cert=pathlib.Path('/etc/letsencrypt/live')/('harness-'+profile)
  shutil.copyfile(cert/'fullchain.pem',etc/'cert.pem');shutil.copyfile(cert/'privkey.pem',etc/'key.pem');public=True
 except Exception: pass
 write('https.pem',(etc/'cert.pem').read_text()+(etc/'key.pem').read_text())
 run([str(target/'sing-box'),'check','-c',str(etc/'sing-box.json')])
 if front:run(['/usr/sbin/haproxy','-c','-f',str(etc/'haproxy.cfg')])
 units={
  'harness-tunnel-node-'+profile+'.service':'[Unit]\nAfter=network-online.target\n[Service]\nType=simple\nExecStart='+str(target/'sing-box')+' run -c /etc/harness-tunnel/sing-box.json\nRestart=always\nRestartSec=2\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n'
 }
 if front:units['harness-tunnel-https-'+profile+'.service']='[Unit]\nAfter=network-online.target harness-tunnel-node-'+profile+'.service\n[Service]\nType=simple\nExecStart=/usr/sbin/haproxy -W -db -f /etc/harness-tunnel/haproxy.cfg\nExecReload=/bin/kill -USR2 $MAINPID\nRestart=always\nRestartSec=2\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n'
 for name,body in units.items():
  path=pathlib.Path('/etc/systemd/system')/name
  if path.exists() and profile not in path.read_text() and str(target) not in path.read_text(): raise RuntimeError('unit collision')
  publish(path,body,0o644)
 run(['systemctl','daemon-reload'])
 for name in units:
  already=subprocess.run(['systemctl','is-active','--quiet',name]).returncode==0
  run(['systemctl','enable',name]);run(['systemctl','restart',name])
  if not already:started.append(name)
  else:restarted.append(name)
 control=target/'control.py'
 publish(control,config['control_script'],0o700)
 # An existing host firewall is never disabled or replaced. UFW has a stable
 # supported owned-rule interface; arbitrary nft/firewalld layouts are reported
 # for external acceptance rather than guessed or globally flushed.
 firewall=json.loads((target/'firewall.json').read_text()) if (target/'firewall.json').is_file() else []
 if pathlib.Path('/usr/sbin/ufw').is_file() and b'Status: active' in run(['/usr/sbin/ufw','status']):
  status=run(['/usr/sbin/ufw','status','numbered']).decode()
  for protocol in ('tcp','udp'):
   label='harness-tunnel-'+profile+'-'+protocol
   if label in status:
    if protocol not in firewall:raise RuntimeError('firewall ownership collision')
    continue
   # Do not take ownership of a pre-existing equivalent allow rule.
   if re.search(r'443/'+protocol+r'.*ALLOW IN',status):continue
   firewall.append(protocol);publish(target/'firewall.json',json.dumps(firewall));firewall_added.append(label)
   run(['/usr/sbin/ufw','insert','1','allow','443/'+protocol,'comment',label])
 publish(target/'firewall.json',json.dumps(firewall))
 user='htun_'+profile[:12]
 if pathlib.Path('/usr/bin/sudo').is_file():
  home=pathlib.Path('/var/lib/harness-tunnel-maint')/profile
  import pwd
  try:
   account=pwd.getpwnam(user)
   if account.pw_dir!=str(home) or account.pw_gecos!='harness-tunnel-'+profile:raise RuntimeError('unowned account collision')
  except KeyError:run(['useradd','--system','--no-create-home','--home-dir',str(home),'--comment','harness-tunnel-'+profile,'--shell','/bin/sh','--password','*',user])
  # sshd opens authorized_keys as this account. The shared parent must allow
  # traversal without exposing the list of other profiles or allowing writes.
  mkdir_owned(home.parent,0o711);home.parent.chmod(0o711)
  mkdir_owned(home,0o755);home.chmod(0o755)
  keys=home/'.ssh';mkdir_owned(keys,0o755)
  authorized='restrict,command="sudo -n /usr/bin/python3 -I -S '+str(control)+'" '+config['maintenance_key']+'\n'
  publish(keys/'authorized_keys',authorized,0o644)
  sudo=pathlib.Path('/etc/sudoers.d')/('harness-tunnel-'+profile)
  publish(sudo,user+' ALL=(root) NOPASSWD: /usr/bin/python3 -I -S '+str(control)+'\n',0o440)
  run(['visudo','-c','-f',str(sudo)])
 else:
  # A root-only minimal Debian image need not install sudo just to keep a
  # restricted control key. Its root SSH admission was already necessary for
  # this bootstrap; the new key still has a fixed command and no forwarding.
  user='root';keys=pathlib.Path('/root/.ssh');mkdir_owned(keys)
  authorized='restrict,command="/usr/bin/python3 -I -S '+str(control)+'" '+config['maintenance_key']+'\n'
  keyfile=keys/'authorized_keys';before=keyfile.read_text() if keyfile.exists() else ''
  if authorized.strip() not in before.splitlines():publish(keyfile,before+('' if not before or before.endswith('\n') else '\n')+authorized)
  publish(target/'root-key.json',json.dumps({'line':authorized.strip()}))
 finished=True
 print(json.dumps({'ok':True,'certificate':(etc/'cert.pem').read_text(),'public_certificate':public,'maintenance_user':user}))
"""


def maintenance_script(profile_id: str) -> str:
    profile_id = identity(profile_id)
    return rf"""import json,os,pathlib,re,shutil,stat,subprocess,sys,tempfile
if os.geteuid()!=0:raise RuntimeError('root required')
def replace_file(path,data,metadata):
 descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name+'.harness-',dir=path.parent)
 try:
  with os.fdopen(descriptor,'wb') as output:
   os.fchown(output.fileno(),metadata.st_uid,metadata.st_gid)
   os.fchmod(output.fileno(),stat.S_IMODE(metadata.st_mode))
   output.write(data);output.flush();os.fsync(output.fileno())
  os.replace(temporary,path)
 finally:
  try:os.unlink(temporary)
  except FileNotFoundError:pass
def remove_owned_directory(directory):
 marker=directory/'owner.json'
 evidence=marker.read_bytes();metadata=marker.stat()
 # Keep ownership intact through all recursive content deletion. The last
 # unlink/rmdir pair restores the exact marker on an ordinary rmdir failure.
 for child in directory.iterdir():
  if child.name=='owner.json':continue
  if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
  else:child.unlink()
 marker.unlink()
 try:directory.rmdir()
 except OSError:
  if directory.is_dir() and not directory.is_symlink():replace_file(marker,evidence,metadata)
  raise
action=sys.stdin.buffer.readline(16).decode('ascii').strip()
if action not in ('status','restart','remove'): sys.exit(2)
target=pathlib.Path('/opt/harness-tunnel/{profile_id}')
etc=pathlib.Path('/etc/harness-tunnel')
# Preflight every known resource before mutating anything. Fresh initial access
# may confirm absence, but authentication failure is never deletion evidence.
for directory in (target,etc):
 if directory.is_symlink():raise RuntimeError('symlink collision')
 if directory.exists():
  marker=directory/'owner.json'
  if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()).get('profile_id')!='{profile_id}':raise RuntimeError('unowned directory')
units=[]
for name in ['harness-tunnel-node-{profile_id}.service','harness-tunnel-https-{profile_id}.service']:
 path=pathlib.Path('/etc/systemd/system')/name
 if path.is_symlink():raise RuntimeError('unit collision')
 if path.exists():
  body=path.read_text()
  expected='ExecStart='+str(target/'sing-box')+' run -c /etc/harness-tunnel/sing-box.json' if '-node-' in name else 'ExecStart=/usr/sbin/haproxy -W -db -f /etc/harness-tunnel/haproxy.cfg'
  if expected not in body or ('-https-' in name and not (etc/'owner.json').is_file()):raise RuntimeError('unit collision')
  units.append(name)
sudo=pathlib.Path('/etc/sudoers.d/harness-tunnel-{profile_id}')
expected='htun_{profile_id[:12]} ALL=(root) NOPASSWD: /usr/bin/python3 -I -S '+str(target/'control.py')+'\n'
if sudo.is_symlink() or (sudo.exists() and sudo.read_text()!=expected):raise RuntimeError('sudo collision')
owned_key_files=[(pathlib.Path('/root/.ssh/authorized_keys'),'restrict,command="/usr/bin/python3 -I -S '+str(target/'control.py')+'" '),(pathlib.Path('/var/lib/harness-tunnel-maint/{profile_id}/.ssh/authorized_keys'),'restrict,command="sudo -n /usr/bin/python3 -I -S '+str(target/'control.py')+'" ')]
for path,prefix in owned_key_files:
 if path.is_symlink() or (path.exists() and not path.is_file()):raise RuntimeError('key collision')
if action=='status':
 ok=bool(units) and all(subprocess.run(['systemctl','is-active','--quiet',name]).returncode==0 for name in units)
 print(json.dumps({{'ok':ok}}));sys.exit(0 if ok else 1)
if action=='restart':
 for name in units: subprocess.run(['systemctl','restart',name],check=True)
 sys.exit(0)
for name in reversed(units):
 subprocess.run(['systemctl','disable','--now',name],check=True)
 (pathlib.Path('/etc/systemd/system')/name).unlink()
subprocess.run(['systemctl','daemon-reload'],check=True)
firewall=target/'firewall.json'
protocols=json.loads(firewall.read_text()) if firewall.is_file() else []
if protocols:
 status=subprocess.check_output(['/usr/sbin/ufw','status','numbered']).decode()
 for protocol in protocols:
  if protocol in ('tcp','udp') and 'harness-tunnel-{profile_id}-'+protocol in status:
   numbers=[int(match.group(1)) for line in status.splitlines() if 'harness-tunnel-{profile_id}-'+protocol in line for match in [re.match(r'\[\s*(\d+)\]',line)] if match]
   for number in sorted(numbers,reverse=True):subprocess.run(['/usr/sbin/ufw','--force','delete',str(number)],check=True)
   status=subprocess.check_output(['/usr/sbin/ufw','status','numbered']).decode()
# Remove only lines admitting this exact fixed helper; preserve all other keys.
for path,prefix in owned_key_files:
 if path.is_file():
  metadata=path.stat();before=path.read_bytes()
  after=b''.join(line for line in before.splitlines(keepends=True) if not line.startswith(prefix.encode('ascii')))
  if after!=before:replace_file(path,after,metadata)
sudo.unlink(missing_ok=True)
if etc.exists():remove_owned_directory(etc)
if target.exists():remove_owned_directory(target)
print(json.dumps({{'ok':True}}))
"""

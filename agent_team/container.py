"""Container test runner. Missing runtime is an error, never a host fallback."""
import os
import shutil
import subprocess
from pathlib import Path

PIDS_LIMIT='128'
MEMORY_LIMIT='512m'
CPU_LIMIT='1'
TIMEOUT_SECONDS=30
SECRET_MARKERS=('SECRET','TOKEN','PASSWORD','API_KEY','ACCESS_KEY','PRIVATE_KEY')

class ContainerUnavailable(RuntimeError):
    pass

def runtime_path():
    return shutil.which('docker') or shutil.which('podman')

def image_name():
    return os.environ.get('AGENT_TEAM_TEST_IMAGE','python:3.11-slim')

def client_env():
    allow={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','LANG','PATHEXT','COMSPEC','PROGRAMDATA','DOCKER_HOST','SYSTEMDRIVE'}
    env={}
    for key,value in os.environ.items():
        upper=key.upper()
        if upper not in allow or any(marker in upper for marker in SECRET_MARKERS):
            continue
        env[key]=value
    return env

def describe_command(workspace,suite_dir,runtime=None,image=None):
    root=str(Path(workspace).resolve())
    return [
        runtime or runtime_path() or 'docker','run','--rm',
        '--network','none',
        '--cap-drop','ALL',
        '--security-opt','no-new-privileges',
        '--pids-limit',PIDS_LIMIT,
        '--memory',MEMORY_LIMIT,
        '--cpus',CPU_LIMIT,
        '--read-only',
        '--tmpfs','/tmp:rw,nosuid,size=64m',
        '--user','65534:65534',
        '-v',f'{root}:/work:ro',
        '-w','/work',
        '-e','PYTHONDONTWRITEBYTECODE=1',
        '-e','PYTHONUNBUFFERED=1',
        image or image_name(),
        'python','-B','-m','unittest','discover','-s',suite_dir,'-v',
    ]

def _reject_secret_leak(argv,env):
    blob=' '.join(argv)+'\n'+'\n'.join(f'{key}={value}' for key,value in env.items())
    for key,value in os.environ.items():
        if value and any(marker in key.upper() for marker in SECRET_MARKERS) and value in blob:
            raise ContainerUnavailable('容器命令携带了宿主机密钥，已拒绝执行')

def ensure_ready():
    runtime=runtime_path()
    if not runtime:
        raise ContainerUnavailable('未找到 docker 或 podman。容器测试执行器无法启动，已拒绝回退到宿主机执行。')
    image=image_name()
    env=client_env()
    try:
        inspect=subprocess.run([runtime,'image','inspect',image],env=env,shell=False,capture_output=True,text=True,timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ContainerUnavailable('检查容器镜像超时，已拒绝回退到宿主机执行') from exc
    if inspect.returncode!=0:
        detail=(inspect.stderr or inspect.stdout or '').strip()
        lowered=detail.lower()
        daemon_down=('cannot connect' in lowered or 'daemon' in lowered or 'npipe' in lowered
            or 'connection refused' in lowered or 'pipe' in lowered)
        if daemon_down:
            raise ContainerUnavailable(f'容器运行环境不可用：{detail}。已拒绝回退到宿主机执行。')
        raise ContainerUnavailable(f'本地没有容器镜像 {image}。为保持默认断网，不会拉取镜像，也不会改在宿主机执行。')
    return runtime,image,env

def run_suite(workspace,suite_dir):
    runtime,image,env=ensure_ready()
    argv=describe_command(workspace,suite_dir,runtime,image)
    _reject_secret_leak(argv,env)
    try:
        result=subprocess.run(argv,env=env,shell=False,capture_output=True,text=True,timeout=TIMEOUT_SECONDS)
        return result.returncode,(result.stdout+result.stderr)[-20000:]
    except subprocess.TimeoutExpired:
        return 124,f'Container execution timed out after {TIMEOUT_SECONDS} seconds'

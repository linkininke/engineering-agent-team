"""Linked worktrees for imported repositories. Never changes the original working tree."""
import os
import subprocess
from pathlib import Path

class GitError(RuntimeError):
    pass

def git_env():
    keep={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','LANG','PATHEXT','COMSPEC','SYSTEMDRIVE'}
    env={k:v for k,v in os.environ.items() if k.upper() in keep}
    env['GIT_TERMINAL_PROMPT']='0'
    env['GCM_INTERACTIVE']='Never'
    return env

def run_git(args,cwd=None,data=None,check=True):
    try:
        result=subprocess.run(['git',*args],cwd=None if cwd is None else str(cwd),input=data,env=git_env(),shell=False,capture_output=True,timeout=30)
    except FileNotFoundError as exc:
        raise GitError('未找到 git，无法操作仓库') from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError('git 命令超过 30 秒') from exc
    if check and result.returncode!=0:
        message=(result.stderr or result.stdout).decode('utf-8','replace').strip()
        raise GitError(message or 'git 命令失败')
    return result

def text(result):
    return result.stdout.decode('utf-8','replace').strip()

def inspect_repo(path):
    path=Path(path).resolve()
    if not path.is_dir():
        raise GitError(f'不是目录：{path}')
    top=Path(text(run_git(['rev-parse','--show-toplevel'],cwd=path))).resolve()
    if text(run_git(['rev-parse','--is-inside-work-tree'],cwd=top))!='true':
        raise GitError('路径不是 Git 工作区')
    head=run_git(['rev-parse','--verify','HEAD'],cwd=top,check=False)
    if head.returncode!=0:
        raise GitError('仓库还没有任何提交，无法建立基线版本')
    porcelain=run_git(['status','--porcelain'],cwd=top).stdout
    return {'root':top,'commit':text(head),'branch':text(run_git(['branch','--show-current'],cwd=top)),'dirty':bool(porcelain.strip()),'porcelain':porcelain}

def fingerprint(origin):
    return (
        run_git(['status','--porcelain'],cwd=origin).stdout,
        run_git(['branch','--show-current'],cwd=origin).stdout,
        run_git(['rev-parse','HEAD'],cwd=origin).stdout,
    )

def head_commit(dest):
    dest=Path(dest)
    if not (dest/'.git').exists():
        return None
    return text(run_git(['rev-parse','HEAD'],cwd=dest))

def add_worktree(origin,dest,branch,commit):
    dest=Path(dest)
    if dest.exists():
        raise GitError(f'工作区路径已存在：{dest}')
    try:
        dest.resolve().relative_to(Path(origin).resolve())
    except ValueError:
        pass
    else:
        raise GitError('任务目录不能放在被导入的仓库内部，请把 --home 设到仓库之外')
    dest.parent.mkdir(parents=True,exist_ok=True)
    run_git(['worktree','add','-b',branch,str(dest),commit],cwd=origin)

def remove_worktree(origin,dest,branch):
    if not origin:
        return
    run_git(['worktree','remove','--force',str(dest)],cwd=origin,check=False)
    if branch:
        run_git(['branch','-D',branch],cwd=origin,check=False)
    run_git(['worktree','prune'],cwd=origin,check=False)

def _rel(rel,allow_hidden):
    path=Path(rel)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise GitError(f'拒绝复制路径：{rel}')
    if any(part=='.git' or part=='__pycache__' for part in path.parts):
        raise GitError(f'拒绝复制控制路径：{rel}')
    if not allow_hidden and any(part.startswith('.') for part in path.parts):
        raise GitError(f'拒绝复制隐藏或控制路径：{rel}')
    return path.as_posix()

def _copy_file(source,target):
    if source.is_symlink() or not source.is_file():
        raise GitError(f'拒绝复制非常规文件：{source}')
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(source.read_bytes())

def import_uncommitted(origin,dest):
    """Copy dirty state into the task worktree and commit it only on the task branch."""
    changed=text(run_git(['diff','--name-only','--no-renames','HEAD'],cwd=origin))
    for rel in changed.splitlines():
        name=_rel(rel.strip().replace('\\','/'),allow_hidden=True)
        source=Path(origin)/name
        target=Path(dest)/name
        if not source.exists():
            if target.is_file() and not target.is_symlink():
                target.unlink()
            continue
        _copy_file(source,target)
    listing=text(run_git(['ls-files','--others','--exclude-standard'],cwd=origin))
    for rel in listing.splitlines():
        name=_rel(rel.strip().replace('\\','/'),allow_hidden=False)
        _copy_file(Path(origin)/name,Path(dest)/name)
    if run_git(['status','--porcelain'],cwd=dest).stdout.strip():
        run_git(['add','-A'],cwd=dest)
        run_git(['-c','user.name=Agent Team','-c','user.email=agent-team@localhost','commit','-m','Import uncommitted snapshot onto task branch'],cwd=dest)
    return head_commit(dest)

def commit_paths(dest,paths,message):
    dest=Path(dest)
    if not (dest/'.git').exists():
        return None
    if paths:
        run_git(['add','--',*paths],cwd=dest)
    if run_git(['diff','--cached','--quiet'],cwd=dest,check=False).returncode==0:
        return head_commit(dest)
    message=' '.join((message or 'Apply approved patch').split())[:200] or 'Apply approved patch'
    run_git(['-c','user.name=Agent Team','-c','user.email=agent-team@localhost','commit','-m',message],cwd=dest)
    return head_commit(dest)

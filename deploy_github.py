#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键把本项目部署到 GitHub（建仓库 -> 推送 -> 开 Pages -> 触发首次抓取）。

设计要点:
  * 全程不出现明文 token。凭据从系统凭据管理器(git-credential-manager)取,
    只在内存里用, 不写进 .git/config, 不打印。
  * 每一步都是幂等的: 仓库已存在就跳过, 远端已配好就更新, Pages 已开就查状态。
  * 失败时打印 GitHub 的原始错误信息, 不做模糊处理。

用法:
    python deploy_github.py check          # 只检查授权与仓库名占用, 不改任何东西
    python deploy_github.py all            # 完整部署
    python deploy_github.py all --repo xxx # 指定仓库名(默认 autohome-koubei)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# 本机代理对 GitHub 做了 TLS 中间人, 系统证书包里有它的根证书。
# requests 走 OpenSSL, 认这个文件; 系统 curl 是 schannel, 不认 --cacert, 所以这里用 requests。
CA = r"C:\Users\025351\.workbuddy\system-ca-bundle.pem"
if os.path.exists(CA):
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CA)
    os.environ.setdefault("SSL_CERT_FILE", CA)

API = "https://api.github.com"


class Fail(Exception):
    pass


# ---------------------------------------------------------------- 凭据

def git(args, check=True, capture=True, env=None):
    """跑一条 git 命令, 默认失败即抛异常。"""
    r = subprocess.run(["git"] + args, capture_output=capture, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise Fail(f"git {' '.join(args)} 失败:\n{msg}")
    return r


TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "_gh_token.txt")
TOKEN_FILE = os.path.normpath(TOKEN_FILE)


def looks_like_token(t):
    return bool(t) and len(t) >= 20 and " " not in t and "\n" not in t


def read_token():
    """
    按优先级取 token: 本地 token 文件 -> 环境变量 -> 系统凭据管理器。
    拿到的 token 只放在内存里用, 全程不打印、不写进任何配置文件。
    """
    # 1) 用户手动放进来的文件(最省事, 也避免 token 出现在聊天记录里)
    if os.path.isfile(TOKEN_FILE):
        raw = open(TOKEN_FILE, encoding="utf-8-sig", errors="replace").read().strip()
        tok = raw.split()[0] if raw.split() else ""
        if looks_like_token(tok):
            print(f"凭据来源: {TOKEN_FILE}")
            return tok
        if raw:
            raise Fail(f"{TOKEN_FILE} 里的内容不像 token (长度 {len(tok)})")

    # 2) 环境变量
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if looks_like_token(os.environ.get(k, "")):
            print(f"凭据来源: 环境变量 {k}")
            return os.environ[k].strip()

    # 3) 系统凭据管理器(GCM 登录过才有)
    #    必须显式禁止交互, 否则 GCM 会弹窗/等输入, 把脚本卡死。
    inp = "protocol=https\nhost=github.com\n\n"
    genv = dict(os.environ)
    genv["GIT_TERMINAL_PROMPT"] = "0"
    genv["GCM_INTERACTIVE"] = "never"
    try:
        r = subprocess.run(["git", "credential", "fill"], input=inp,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=genv, timeout=15)
    except subprocess.TimeoutExpired:
        r = None
    if r is not None and r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                tok = line.split("=", 1)[1].strip()
                if looks_like_token(tok):
                    print("凭据来源: 系统凭据管理器")
                    return tok

    raise Fail(
        "没有可用的 GitHub token。三选一:\n"
        f"  a) 把 token 粘进这个文件(推荐): {TOKEN_FILE}\n"
        "  b) 设置环境变量 GITHUB_TOKEN\n"
        "  c) git-credential-manager github login\n"
        "token 需要 repo + workflow 两个权限。"
    )


def session(token):
    import requests
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "koubei-deploy",
    })
    return s


def api(s, method, path, ok=(200, 201, 204), **kw):
    """统一的 API 调用, 把 GitHub 的报错原样带出来。"""
    r = s.request(method, API + path, timeout=30, **kw)
    if r.status_code in ok:
        return r
    raise Fail(f"{method} {path} -> HTTP {r.status_code}\n{r.text[:600]}")


# ---------------------------------------------------------------- 步骤

def step_check(s, repo_name):
    r = api(s, "GET", "/user")
    me = r.json()
    scopes = r.headers.get("x-oauth-scopes", "")
    print(f"  登录账号   : {me['login']}  ({me.get('name') or '无昵称'})")
    print(f"  账号类型   : {me['type']}")
    print(f"  token 权限 : {scopes or '(未返回, 可能是 fine-grained)'}")

    # 推 .github/workflows/ 需要 workflow 权限, 缺了会在 push 时被拒
    if scopes and "workflow" not in [x.strip() for x in scopes.split(",")]:
        print("  !! 缺少 workflow 权限, 推送 Actions 文件会被拒绝")

    exists = s.get(f"{API}/repos/{me['login']}/{repo_name}", timeout=20)
    if exists.status_code == 200:
        d = exists.json()
        print(f"  仓库         : {d['full_name']} 已存在 "
              f"({'私有' if d['private'] else '公开'}), 默认分支 {d.get('default_branch')}")
    elif exists.status_code == 404:
        print(f"  仓库         : {me['login']}/{repo_name} 不存在, 将新建(公开)")
    else:
        print(f"  仓库         : 查询返回 HTTP {exists.status_code}")
    return me["login"]


def step_create(s, owner, repo_name, private):
    r = s.get(f"{API}/repos/{owner}/{repo_name}", timeout=20)
    if r.status_code == 200:
        print(f"  仓库已存在, 跳过创建: {r.json()['full_name']}")
        return r.json()["full_name"]
    payload = {
        "name": repo_name,
        "description": "汽车之家口碑抓取（GitHub Actions 执行 + Pages 展示）",
        "private": private,
        "has_issues": True,      # 别人靠开 issue 触发抓取, 必须开
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,      # 不要初始 commit, 否则 push 会冲突
    }
    r = api(s, "POST", "/user/repos", json=payload)
    d = r.json()
    print(f"  已创建: {d['full_name']} ({'私有' if d['private'] else '公开'})")
    return d["full_name"]


def step_push(owner, repo_name, token=None):
    url = f"https://github.com/{owner}/{repo_name}.git"
    remotes = git(["remote"]).stdout.split()
    if "origin" in remotes:
        git(["remote", "set-url", "origin", url])
    else:
        git(["remote", "add", "origin", url])

    branch = git(["branch", "--show-current"]).stdout.strip() or "main"

    # 凭据通过环境变量交给一个临时 helper 脚本。
    # 刻意不把 token 拼进 URL 或 argv —— 那样会留在 .git/config 里、也会被进程列表看到。
    env = dict(os.environ)
    pre = []
    if token:
        env["GH_TOKEN"] = token
        helper = ('!f() { if [ "$1" = get ]; then '
                  'echo username=x-access-token; echo "password=$GH_TOKEN"; fi; }; f')
        pre = ["-c", "credential.helper=", "-c", "credential.helper=" + helper]
    else:
        git(["config", "--local", "credential.helper", "manager"])

    print(f"  推送 {branch} -> {url}")
    r = git(pre + ["push", "-u", "origin", branch], check=False, env=env)
    if r.returncode != 0:
        err = (r.stderr or "") + (r.stdout or "")
        low = err.lower()
        if "workflow" in low and ("refus" in low or "scope" in low):
            raise Fail("推送被拒: token 缺少 workflow 权限。\n"
                       "重新生成 token 时务必勾选 workflow(创建 .github/workflows/ 下的文件必需)。\n"
                       f"原始错误:\n{err[:400]}")
        if "403" in err or "denied" in low:
            raise Fail(f"推送被拒(权限问题):\n{err[:500]}")
        raise Fail(f"推送失败:\n{err[:600]}")
    print("  推送完成")


def wait_branch(s, full, branch="main", timeout=60):
    """刚建的空仓库, API 有时要几百毫秒才认到新分支。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = s.get(f"{API}/repos/{full}/branches/{branch}", timeout=20)
        if r.status_code == 200:
            return True
        time.sleep(3)
    raise Fail(f"等待分支 {branch} 超时")


def step_pages(s, full):
    r = s.get(f"{API}/repos/{full}/pages", timeout=20)
    if r.status_code == 200:
        d = r.json()
        print(f"  Pages 已开启: {d.get('html_url')}")
        src = d.get("source") or {}
        print(f"  源: {src.get('branch')} / {src.get('path')}")
        return d.get("html_url")

    # 403 一般是免费账号 + 私有仓库
    payload = {"source": {"branch": "main", "path": "/docs"},
               "build_type": "legacy"}
    r = s.post(f"{API}/repos/{full}/pages", json=payload, timeout=30)
    if r.status_code not in (200, 201):
        if r.status_code == 403:
            raise Fail("开启 Pages 被拒(403)。免费账号的 Pages 只支持公开仓库;\n"
                       "若仓库是私有的, 需要 GitHub Pro, 或改用 Actions Artifacts 下载 CSV。\n"
                       f"{r.text[:300]}")
        raise Fail(f"开启 Pages 失败 HTTP {r.status_code}\n{r.text[:400]}")
    d = r.json()
    print(f"  已开启: {d.get('html_url')}")
    return d.get("html_url")


def wait_pages(s, full, timeout=300):
    """等首次构建结束。Pages 首次构建常要 1-3 分钟。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        r = s.get(f"{API}/repos/{full}/pages/builds/latest", timeout=20)
        if r.status_code == 200:
            b = r.json()
            st = b.get("status")
            if st != last:
                last = st
                print(f"    [{int(time.time()-t0):3}s] Pages 构建: {st}")
            if st == "built":
                return True
            if st == "errored":
                raise Fail("Pages 构建失败, 去仓库 Actions 页看 build 日志")
        else:
            # 没有 build 记录时 Pages 仍可能已可访问
            pass
        time.sleep(8)
    print("    构建状态未确认(超时), 但页面可能已可访问")
    return False


def workflow_id(s, full):
    r = s.get(f"{API}/repos/{full}/actions/workflows", timeout=20)
    if r.status_code != 200:
        return None
    for w in r.json().get("workflows", []):
        if w["path"].endswith("crawl.yml"):
            return w["id"]
    return None


def step_dispatch(s, full, series, pages):
    wid = workflow_id(s, full)
    if wid is None:
        # Actions 可能还没索引到 workflow, 稍等重试
        for _ in range(5):
            time.sleep(5)
            wid = workflow_id(s, full)
            if wid:
                break
    if wid is None:
        print("  !! 仓库里还没索引到 crawl.yml, 请稍后到 Actions 页面手动 Run workflow")
        return None
    api(s, "POST", f"/repos/{full}/actions/workflows/{wid}/dispatches",
        json={"ref": "main",
              # 只允许传 workflow_dispatch.inputs 里声明过的键, 多传一个 GitHub 就回 422
              "inputs": {"series": series, "pages": str(pages),
                         "full_text": "true"}})
    print(f"  已触发 workflow: {series} / {pages} 页")
    return wid


def _parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def step_watch_run(s, full, since=None, timeout=900):
    """
    等这次派发出去的任务跑完。

    不能用"列表里最新的一条"判断: 刚 dispatch 完, Actions 索引要几秒才更新,
    这时列表里最新的还是上一次那条已完成的运行, 会立刻误报 success;
    同时也不能退而求其次拿更老的一条。所以按 created_at 卡在派发时刻之后。
    """
    if since is None:
        since = datetime.now(timezone.utc)
    since = since - timedelta(seconds=15)   # 容忍本机与 GitHub 之间的时钟偏差

    t0 = time.time()
    run = None
    while time.time() - t0 < 90:
        r = s.get(f"{API}/repos/{full}/actions/runs",
                  params={"event": "workflow_dispatch", "per_page": 10}, timeout=20)
        runs = r.json().get("workflow_runs", []) if r.status_code == 200 else []
        fresh = [x for x in runs if _parse_iso(x["created_at"]) >= since]
        if fresh:
            run = fresh[0]
            break
        time.sleep(5)
    if not run:
        print("  !! 没查到新的运行记录, 去 Actions 页面看")
        return None

    print(f"  运行 #{run['run_number']}: {run['html_url']}")
    last = ""
    while time.time() - t0 < timeout:
        r = s.get(f"{API}/repos/{full}/actions/runs/{run['id']}", timeout=20)
        d = r.json()
        st = f"{d.get('status')}/{d.get('conclusion') or '-'}"
        if st != last:
            last = st
            print(f"    [{int(time.time()-t0):3}s] {st}")
        if d.get("status") == "completed":
            return d
        time.sleep(10)
    print("  等待超时, 任务可能仍在跑")
    return None


def step_labels(s, full):
    """给 issue 加一个「申请抓取」标签, 方便别人一眼看出怎么用。"""
    r = s.get(f"{API}/repos/{full}/labels/crawl", timeout=20)
    if r.status_code == 404:
        s.post(f"{API}/repos/{full}/labels",
               json={"name": "crawl", "color": "1f883d",
                     "description": "申请抓取车型口碑"}, timeout=20)


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["check", "all", "run"], nargs="?", default="all")
    ap.add_argument("--repo", default="autohome-koubei")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--series", default="零跑C10")
    ap.add_argument("--pages", default="3")
    ap.add_argument("--no-run", action="store_true", help="不触发首次抓取")
    a = ap.parse_args()

    print("=" * 62)
    token = read_token()
    s = session(token)

    print("\n[1/6] 校验账号与仓库名")
    owner = step_check(s, a.repo)
    if a.stage == "check":
        print("\ncheck 完成, 未做任何修改。")
        return 0

    if a.stage == "run":
        # 只重跑"触发抓取"这一步, 不重复建仓库/推代码/等 Pages
        full = f"{owner}/{a.repo}"
        print(f"\n触发抓取: {full} / {a.series} / {a.pages} 页")
        since = datetime.now(timezone.utc)
        step_dispatch(s, full, a.series, a.pages)
        run = step_watch_run(s, full, since=since)
        r = s.get(f"{API}/repos/{full}/pages", timeout=20)
        site = (r.json().get("html_url") if r.status_code == 200 else "")
        print("\n" + "=" * 62)
        print(f"  仓库   : https://github.com/{full}")
        print(f"  数据页 : {site}")
        print(f"  运行   : {run.get('conclusion') if run else '未确认'}  "
              f"{(run or {}).get('html_url','')}")
        print("=" * 62)
        return 0

    print("\n[2/6] 创建仓库")
    full = step_create(s, owner, a.repo, a.private)

    print("\n[3/6] 推送代码")
    step_push(owner, a.repo, token)
    wait_branch(s, full)

    print("\n[4/6] 开启 Pages")
    site = step_pages(s, full)

    print("\n[5/6] 等待 Pages 首次构建")
    wait_pages(s, full)

    print("\n[6/6] 触发首次抓取")
    if a.no_run:
        print("  已跳过(--no-run)")
        run = None
    else:
        since = datetime.now(timezone.utc)
        step_dispatch(s, full, a.series, a.pages)
        run = step_watch_run(s, full, since=since)
    step_labels(s, full)

    print("\n" + "=" * 62)
    print("部署完成")
    print(f"  仓库     : https://github.com/{full}")
    print(f"  数据页   : {site}")
    print(f"  Actions  : https://github.com/{full}/actions/workflows/crawl.yml")
    print(f"  申请入口 : https://github.com/{full}/issues/new"
          f"?title=%5B%E6%8A%93%E5%8F%96%5D%20%E9%9B%B6%E8%B7%91C10")
    if run:
        print(f"  首次运行 : {run.get('conclusion')}  {run.get('html_url')}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"\n[失败] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

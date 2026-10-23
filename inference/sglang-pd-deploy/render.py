#!/usr/bin/env python3
"""deploy.toml -> 校验 + 生成启动脚本 / 预检与诊断脚本 / plan.md / params.md / plan.json。

用法: python render.py <deploy.toml> [-o <out_dir>] [--params <params.json>]

规格见仓库 DESIGN.md 第 3、4、6 节（v2：场景简写 / 跨节点实例 / 端口自动分配；v3：最小预检 / 诊断脚本 / 容器内检查 / params.md / params.json + --params）。Python >= 3.11，只用标准库。
校验失败: 所有错误一次性打印到 stderr，退出码 2，不生成任何文件。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path, PurePosixPath

SKILL_DIR = Path(__file__).resolve().parent
BASELINE_TOML = SKILL_DIR / "deploy.example.toml"
DEEPEP_DEFAULT = SKILL_DIR / "deepep-config.json"

ROLES = ("prefill", "decode")
PULL_POLICIES = ("if_not_present", "always", "never")
ON_CONFLICTS = ("reuse", "abort", "recreate")
KNOWN_SECTIONS = {"deploy", "nodes", "network", "image", "container", "model", "run",
                  "mooncake", "router", "prefill", "decode", "env", "args", "topology", "ports"}
TOPOLOGY_KEYS = {"prefill", "decode", "prefill_cards", "decode_cards", "cards_per_node", "nodes"}
CARDS_TOKEN = "@cards"
PARAMS_TOP_KEYS = ("_readme", "env", "args", "instances", "mooncake", "router", "derived")
PARAMS_IGNORED = ("_readme", "derived")
PARAMS_README = ("可修改 env / args / instances；值 true=开关，false=删除该参数，\"@cards\"=实例总卡数。"
                 "derived 段只读，改了无效。")
VALIDATED_CARDS = 4   # 文档验证场景：每实例单节点 4 卡
DIST_INIT_STRIDE = 100  # sglang DP 会使用 dist-init 后续内部端口，自动端口留出间隔
DIST_INIT_RESERVED = 8  # 校验显式端口时也为每个 dist-init 预留这个端口区间

# 派生项（模板头注释里的列表 + served-model-name）
DERIVED_ENV = (
    "HIP_VISIBLE_DEVICES", "NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "NCCL_IB_HCA",
    "UCX_NET_DEVICES", "MC_ALLOWED_IBV_DEVICES", "SGLANG_TORCH_PROFILER_DIR",
    "MOONCAKE_LOCAL_HOSTNAME", "MOONCAKE_TE_META_DATA_SERVER", "MOONCAKE_MASTER",
    "MOONCAKE_GLOBAL_SEGMENT_SIZE",
)
DERIVED_ARGS = (
    "port", "host", "model-path", "speculative-draft-model-path", "served-model-name",
    "dist-init-addr", "nnodes", "node-rank", "deepep-config", "disaggregation-mode",
    "disaggregation-bootstrap-port", "disaggregation-ib-device",
)
ERROR_PATTERN = ("Traceback|RuntimeError|out of memory|OutOfMemory|HIP error|CUDA error|"
                 "Segmentation fault|core dumped|Address already in use|Killed")
KNOWN_NONFATAL_ERROR_PATTERN = "multimem all-gather disabled"
SERVER_READY_MARKER = "The server is fired up and ready to roll!"
SERVER_CMD = "sglang serve"
ROUTER_CMD = "python -m sglang_router.launch_router"
TOOLS_CHECK = 'command -v sglang curl; python -c "import sglang_router" && echo router_ok'
MOONCAKE_TOOLS_CHECK = ("command -v mooncake_master && mooncake_master --help 2>&1 | "
                        "grep -q rpc_port && echo rpc_port_ok")

NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ARG_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SEGMENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB|TB|TiB)", re.I)
HEX_ID_RE = re.compile(r"[0-9a-fA-F]{12,64}")

q = shlex.quote


# ───────────────────────── 数据结构 ─────────────────────────

@dataclass
class Node:
    alias: str
    mcp_host: str
    ip: str
    ifname: str
    ib_devices: list
    ib_port: int


@dataclass
class Member:
    """实例在某个节点上的一份（rank = node-rank）。"""
    rank: int
    node: str
    cards: list
    name: str    # 脚本 / pid / log 名：单节点 = 实例名，跨节点 = <实例名>_r<rank>


@dataclass
class Spec:
    """展开后的实例规格（两种写法统一成它）。"""
    role: str
    idx: int
    members: list                                   # [(节点别名, cards)]
    ports: dict = field(default_factory=dict)       # 显式写死的端口
    env: dict = field(default_factory=dict)         # 实例级覆盖（已规范化）
    args: dict = field(default_factory=dict)


@dataclass
class Instance:
    role: str
    index: int
    name: str
    members: list
    port: int
    bootstrap_port: int
    dist_init_port: int
    env: dict    # env.<role> ⊕ 实例 env（不含派生项）
    args: dict   # args.<role> ⊕ 实例 args（"@cards" 已替换，false 已删除）
    args_raw: dict = field(default_factory=dict)    # 同上但 "@cards" 未替换（用于和基线比较）
    inst_env: dict = field(default_factory=dict)    # 实例级原始覆盖
    inst_args: dict = field(default_factory=dict)

    @property
    def total_cards(self) -> int:
        return sum(len(m.cards) for m in self.members)

    @property
    def multi(self) -> bool:
        return len(self.members) > 1

    @property
    def rank0(self) -> Member:
        return self.members[0]


@dataclass
class Config:
    prefix: str = ""
    src_name: str = "deploy.toml"
    nodes: dict = field(default_factory=dict)
    image_ref: str = ""
    pull_policy: str = "if_not_present"
    work_dir: str = ""
    model_root: str = ""
    shm_size: str = "128G"
    extra_args: list = field(default_factory=list)
    on_conflict: str = "reuse"
    model_dir: str = ""
    served: str = ""
    deepep_custom: str = ""
    startup_timeout_s: int = 3600
    poll_interval_s: int = 30
    card_busy_mem_pct: float = 5
    rpc_port: int = 50051
    metadata_port: int = 8080
    segment_size: str = "25GB"
    seg_num: Decimal = Decimal(25)
    seg_unit: str = "GB"
    mc_extra: list = field(default_factory=list)
    router_port: int = 30000
    prefill_policy: str = "round_robin"
    router_extra: list = field(default_factory=list)
    instances: list = field(default_factory=list)
    http_base: int = 10015
    bootstrap_base: int = 8998
    dist_base: int = 10749
    mode: str = ""             # "topology" | "explicit"
    scenario_warning: str = ""
    params_src: str = ""       # --params 文件的绝对路径；空 = 未使用
    role_env: dict = field(default_factory=dict)    # 有效值（false 已删除、键已规范化）
    role_args: dict = field(default_factory=dict)

    @property
    def deploy_dir(self) -> str:
        return f"{self.work_dir}/{self.prefix}"

    @property
    def model_path(self) -> str:
        return f"/model/{self.model_dir}/"

    @property
    def deepep(self) -> str:
        return self.deepep_custom or f"{self.deploy_dir}/deepep-config.json"

    @property
    def prefills(self) -> list:
        return [i for i in self.instances if i.role == "prefill"]

    @property
    def decodes(self) -> list:
        return [i for i in self.instances if i.role == "decode"]

    @property
    def p1(self) -> Instance:
        return self.prefills[0]

    @property
    def total_ranks(self) -> int:
        return sum(i.total_cards for i in self.prefills)

    @property
    def p1_node(self) -> Node:
        """mooncake master / router 所在节点：prefill_1 的 rank0 节点。"""
        return self.nodes[self.p1.rank0.node]

    @property
    def scenario(self) -> str:
        return f"{len(self.prefills)}P{len(self.decodes)}D"

    @property
    def expected_total(self) -> str:
        return f"{fmt_decimal(self.seg_num * self.total_ranks)} {self.seg_unit}"

    @property
    def used_nodes(self) -> list:
        """按 [nodes] 顺序，只保留有 member 的节点。"""
        used = {m.node for i in self.instances for m in i.members}
        return [n for a, n in self.nodes.items() if a in used]

    @property
    def unused_nodes(self) -> list:
        used = {m.node for i in self.instances for m in i.members}
        return [a for a in self.nodes if a not in used]

    def members_on(self, alias: str) -> list:
        """[(实例, member)]：该节点上的所有 member，按实例顺序。"""
        return [(i, m) for i in self.instances for m in i.members if m.node == alias]

    def node_of(self, m: Member) -> Node:
        return self.nodes[m.node]

    @property
    def validated_scenario(self) -> bool:
        """是否等同于文档验证过的场景：所有实例都是单节点 4 卡。"""
        return all(not i.multi and i.total_cards == VALIDATED_CARDS for i in self.instances)


def fmt_decimal(d: Decimal) -> str:
    return format(d.normalize(), "f")


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def to_int(v):
    """int 或纯数字字符串 -> int，其余 None。"""
    if is_int(v):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


# ───────────────────────── 校验 + 派生 ─────────────────────────

def _table(parent, key, path, errors) -> dict:
    v = parent.get(key, {})
    if not isinstance(v, dict):
        errors.append(f"{path} 必须是表")
        return {}
    return v


def _req_str(v, path, errors) -> str:
    if not isinstance(v, str) or not v.strip():
        errors.append(f"{path} 必填")
        return ""
    if "<" in v:
        errors.append(f"{path} 仍含占位符: {v!r}")
        return ""
    return v.strip()


def _port(v, path, errors, default=None) -> int:
    if v is None:
        v = default
    if not is_int(v) or not 1 <= v <= 65535:
        errors.append(f"{path} 必须是 1-65535 的整数，当前: {v!r}")
        return 0
    return v


def _str_list(v, path, errors) -> list:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        errors.append(f"{path} 必须是字符串列表")
        return []
    return list(v)


def _abs_posix(v: str, path: str, errors) -> str:
    if not v:
        return v
    if not v.startswith("/"):
        errors.append(f"{path} 必须是 Linux 绝对路径（以 / 开头）: {v!r}")
    elif ":" in v:
        errors.append(f"{path} 不能含 ':'（会破坏 docker -v）: {v!r}")
    return v.rstrip("/") or "/"


def _norm_kv(tbl, where, kind, errors) -> dict:
    """校验并规范化 env / args 表：键合法、非派生；args 键去掉前导 '-'。"""
    out = {}
    if not isinstance(tbl, dict):
        errors.append(f"{where} 必须是表")
        return out
    for k, v in tbl.items():
        if kind == "env":
            if not ENV_KEY_RE.fullmatch(k):
                errors.append(f"{where}: env 名非法 {k!r}")
                continue
            if k in DERIVED_ENV:
                errors.append(f"{where}: {k} 是派生项，不能手写")
                continue
            if isinstance(v, (dict, list)) or v is None:
                errors.append(f"{where}: {k} 的值必须是标量")
                continue
        else:
            nk = k.lstrip("-")
            if not ARG_KEY_RE.fullmatch(nk):
                errors.append(f"{where}: 参数名非法 {k!r}")
                continue
            if nk in DERIVED_ARGS:
                errors.append(f"{where}: --{nk} 是派生项，不能手写")
                continue
            if isinstance(v, dict) or v is None:
                errors.append(f"{where}: --{nk} 的值不支持表/空值")
                continue
            k = nk
        out[k] = v
    return out


def _effective(d: dict) -> dict:
    """false = 删除该键。"""
    return {k: v for k, v in d.items() if v is not False}


def load_params(path, errors: list) -> dict:
    """读 --params JSON，返回生效的顶层键（去掉 _readme / derived）；出错记入 errors 并返回 {}。"""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except FileNotFoundError:
        errors.append(f"--params 文件不存在: {path}")
        return {}
    except OSError as e:
        errors.append(f"--params 文件读取失败: {e}")
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        errors.append(f"--params JSON 解析失败: {e}")
        return {}
    if not isinstance(data, dict):
        errors.append("--params 顶层必须是 JSON 对象")
        return {}
    for k in data:
        if k not in PARAMS_TOP_KEYS:
            errors.append(f"--params 未知顶层键 {k!r}（可用 {', '.join(PARAMS_TOP_KEYS)}）")
    return {k: v for k, v in data.items() if k in PARAMS_TOP_KEYS and k not in PARAMS_IGNORED}


def _params_table(params: dict, key: str, errors: list):
    """params 里的某个顶层段：不存在 -> None；不是对象 -> 报错并返回 None。"""
    if key not in params:
        return None
    v = params[key]
    if not isinstance(v, dict):
        errors.append(f"params {key} 必须是对象")
        return None
    return v


def _params_keyed(params: dict, key: str, allowed: tuple, errors: list) -> dict:
    """mooncake / router 段：只允许 allowed 里的键。"""
    out = {}
    for k, v in (_params_table(params, key, errors) or {}).items():
        if k in allowed:
            out[k] = v
        else:
            errors.append(f"params {key} 未知键 {k!r}（可用 {' / '.join(allowed)}）")
    return out


def load(toml_path: Path, params_path=None):
    """返回 (cfg, errors, warnings)。errors 非空时 cfg 不可用于生成。

    params_path: 可选的 params.json，覆盖 env / args / instances / mooncake / router（整段替换）。
    """
    errors: list = []
    warnings: list = []
    cfg = Config(src_name=Path(toml_path).name)
    try:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return cfg, [f"文件不存在: {toml_path}"], warnings
    except tomllib.TOMLDecodeError as e:
        return cfg, [f"TOML 解析失败: {e}"], warnings

    params: dict = {}
    if params_path is not None:
        cfg.params_src = str(Path(params_path).resolve())
        params = load_params(params_path, errors)

    for k in raw:
        if k not in KNOWN_SECTIONS:
            warnings.append(f"未知段 [{k}]，已忽略")

    # deploy / image / container / model / run
    deploy = _table(raw, "deploy", "[deploy]", errors)
    cfg.prefix = _req_str(deploy.get("prefix"), "deploy.prefix", errors)
    if cfg.prefix and not NAME_RE.fullmatch(cfg.prefix):
        errors.append(f"deploy.prefix 只允许 [A-Za-z0-9_.-]，当前: {cfg.prefix!r}")

    image = _table(raw, "image", "[image]", errors)
    cfg.image_ref = _req_str(image.get("ref"), "image.ref", errors)
    cfg.pull_policy = image.get("pull_policy", "if_not_present")
    if cfg.pull_policy not in PULL_POLICIES:
        errors.append(f"image.pull_policy 必须是 {'|'.join(PULL_POLICIES)}，当前: {cfg.pull_policy!r}")
    ref = cfg.image_ref
    if ref and (ref.startswith("sha256:") or HEX_ID_RE.fullmatch(ref)) and cfg.pull_policy != "never":
        errors.append(f"image.ref 是镜像 ID（{ref}），pull_policy 只能是 never，当前: {cfg.pull_policy!r}")

    cont = _table(raw, "container", "[container]", errors)
    cfg.work_dir = _abs_posix(_req_str(cont.get("work_dir"), "container.work_dir", errors),
                              "container.work_dir", errors)
    cfg.model_root = _abs_posix(_req_str(cont.get("model_root"), "container.model_root", errors),
                                "container.model_root", errors)
    shm = cont.get("shm_size", "128G")
    cfg.shm_size = str(shm)
    cfg.extra_args = _str_list(cont.get("extra_args"), "container.extra_args", errors)
    cfg.on_conflict = cont.get("on_conflict", "reuse")
    if cfg.on_conflict not in ON_CONFLICTS:
        errors.append(f"container.on_conflict 必须是 {'|'.join(ON_CONFLICTS)}，当前: {cfg.on_conflict!r}")

    model = _table(raw, "model", "[model]", errors)
    mdir = _req_str(model.get("dir"), "model.dir", errors).strip("/")
    cfg.model_dir = mdir
    if model.get("dir") and not mdir and "<" not in str(model.get("dir")):
        errors.append("model.dir 必填")
    served = model.get("served_model_name") or ""
    cfg.served = served if isinstance(served, str) and served else (PurePosixPath(mdir).name if mdir else "")
    dc = model.get("deepep_config") or ""
    if not isinstance(dc, str):
        errors.append("model.deepep_config 必须是字符串")
        dc = ""
    if dc and not dc.startswith("/"):
        errors.append(f"model.deepep_config 必须是容器内绝对路径: {dc!r}")
    cfg.deepep_custom = dc

    run = _table(raw, "run", "[run]", errors)
    cfg.startup_timeout_s = run.get("startup_timeout_s", 3600)
    cfg.poll_interval_s = run.get("poll_interval_s", 30)
    busy_pct = run.get("card_busy_mem_pct", 5)
    if isinstance(busy_pct, bool) or not isinstance(busy_pct, (int, float)) or not 0 <= busy_pct <= 100:
        errors.append(f"run.card_busy_mem_pct 必须是 0-100 的数字，当前: {busy_pct!r}")
    else:
        cfg.card_busy_mem_pct = busy_pct

    # mooncake / router
    mc = _table(raw, "mooncake", "[mooncake]", errors)
    cfg.rpc_port = _port(mc.get("rpc_port"), "mooncake.rpc_port", errors, 50051)
    cfg.metadata_port = _port(mc.get("metadata_port"), "mooncake.metadata_port", errors, 8080)
    mc_over = _params_keyed(params, "mooncake", ("segment_size", "extra_args"), errors)
    cfg.segment_size = str(mc_over.get("segment_size", mc.get("segment_size", "25GB")))
    m = SEGMENT_RE.fullmatch(cfg.segment_size.strip())
    if not m:
        errors.append(f"mooncake.segment_size 须形如 <数字><GB|GiB|MB|MiB|TB|TiB>，当前: {cfg.segment_size!r}")
    else:
        cfg.seg_num = Decimal(m.group(1))
        cfg.seg_unit = m.group(2)
    if "extra_args" in mc_over:
        cfg.mc_extra = _str_list(mc_over["extra_args"], "params mooncake.extra_args", errors)
    else:
        cfg.mc_extra = _str_list(mc.get("extra_args"), "mooncake.extra_args", errors)

    rt = _table(raw, "router", "[router]", errors)
    cfg.router_port = _port(rt.get("port"), "router.port", errors, 30000)
    rt_over = _params_keyed(params, "router", ("prefill_policy", "extra_args"), errors)
    if "prefill_policy" in rt_over and not isinstance(rt_over["prefill_policy"], str):
        errors.append(f"params router.prefill_policy 必须是字符串，当前: {rt_over['prefill_policy']!r}")
        rt_over.pop("prefill_policy")
    cfg.prefill_policy = str(rt_over.get("prefill_policy", rt.get("prefill_policy", "round_robin")))
    if "extra_args" in rt_over:
        cfg.router_extra = _str_list(rt_over["extra_args"], "params router.extra_args", errors)
    else:
        cfg.router_extra = _str_list(rt.get("extra_args"), "router.extra_args", errors)

    # 网络 + 节点
    net_def = _table(raw, "network", "[network]", errors)
    nodes_raw = _table(raw, "nodes", "[nodes]", errors)
    if not nodes_raw:
        errors.append("至少需要定义 1 个 [nodes.<别名>]")
    for alias, nd in nodes_raw.items():
        path = f"nodes.{alias}"
        if not isinstance(nd, dict):
            errors.append(f"{path} 必须是表")
            continue
        mcp_host = _req_str(nd.get("mcp_host"), f"{path}.mcp_host", errors)
        ip = _req_str(nd.get("ip"), f"{path}.ip", errors)
        nn = nd.get("network", {})
        if not isinstance(nn, dict):
            errors.append(f"{path}.network 必须是表")
            nn = {}
        net = {**net_def, **nn}
        ifname = net.get("socket_ifname")
        ibs = net.get("ib_devices")
        ibp = net.get("ib_port", 1)
        if not isinstance(ifname, str) or not ifname:
            errors.append(f"{path}: socket_ifname 必填（[network] 或 {path}.network）")
            ifname = ""
        if not isinstance(ibs, list) or not ibs or not all(isinstance(x, str) and x for x in ibs):
            errors.append(f"{path}: ib_devices 必须是非空字符串列表")
            ibs = []
        if not is_int(ibp) or ibp < 0:
            errors.append(f"{path}: ib_port 必须是非负整数")
            ibp = 1
        cfg.nodes[alias] = Node(alias, mcp_host, ip, ifname, ibs, ibp)

    # env / args 角色默认值
    env_raw = _table(raw, "env", "[env]", errors)
    args_raw = _table(raw, "args", "[args]", errors)
    for name, tbl in (("env", env_raw), ("args", args_raw)):
        for k, v in tbl.items():
            if not isinstance(v, dict):
                errors.append(f"[{name}] 下只能有 [{name}.prefill] / [{name}.decode] 子表，发现键 {k!r}")
            elif k not in ROLES:
                warnings.append(f"[{name}.{k}] 不是 prefill/decode，已忽略")
    p_kv = {}
    for name in ("env", "args"):
        p_kv[name] = _params_table(params, name, errors) or {}
        for k, v in p_kv[name].items():
            if k not in ROLES:
                errors.append(f"params {name}: 未知角色 {k!r}（只能是 prefill / decode）")
            elif not isinstance(v, dict):
                errors.append(f"params {name}.{k} 必须是对象")
    role_env_raw, role_args_raw = {}, {}
    for role in ROLES:
        if isinstance(p_kv["env"].get(role), dict):
            role_env_raw[role] = _norm_kv(p_kv["env"][role], f"params env.{role}", "env", errors)
        else:
            role_env_raw[role] = _norm_kv(env_raw.get(role, {}), f"[env.{role}]", "env", errors)
        if isinstance(p_kv["args"].get(role), dict):
            role_args_raw[role] = _norm_kv(p_kv["args"][role], f"params args.{role}", "args", errors)
        else:
            role_args_raw[role] = _norm_kv(args_raw.get(role, {}), f"[args.{role}]", "args", errors)
        cfg.role_env[role] = _effective(role_env_raw[role])
        cfg.role_args[role] = _effective(role_args_raw[role])

    # [ports] / 实例展开
    ports_t = _table(raw, "ports", "[ports]", errors)
    for k in ports_t:
        if k not in ("http_base", "bootstrap_base", "dist_init_base"):
            errors.append(f"[ports] 未知键 {k!r}（可用 http_base / bootstrap_base / dist_init_base）")
    cfg.http_base = ports_t.get("http_base", cfg.http_base)
    cfg.bootstrap_base = ports_t.get("bootstrap_base", cfg.bootstrap_base)
    cfg.dist_base = ports_t.get("dist_init_base", cfg.dist_base)
    for k, v in (("http_base", cfg.http_base), ("bootstrap_base", cfg.bootstrap_base),
                 ("dist_init_base", cfg.dist_base)):
        if not is_int(v) or not 1 <= v <= 65535:
            errors.append(f"ports.{k} 必须是 1-65535 的整数，当前: {v!r}")

    specs = _expand_specs(raw, cfg, errors)
    _apply_instance_params(params, specs, cfg, errors)
    for gi, sp in enumerate(specs):
        where = f"{sp.role}_{sp.idx}"
        auto = {"port": cfg.http_base, "bootstrap_port": cfg.bootstrap_base,
                "dist_init_port": cfg.dist_base}
        ports = {}
        for key, base in auto.items():
            if key in sp.ports:
                ports[key] = _port(sp.ports[key], f"{where}.{key}", errors)
            elif is_int(base):
                step = DIST_INIT_STRIDE if key == "dist_init_port" else 1
                offset = gi * step
                ports[key] = _port(base + offset,
                                   f"{where}.{key}（自动分配 base + {offset}）", errors)
            else:
                ports[key] = 0
        env = _effective({**role_env_raw[sp.role], **sp.env})
        args_raw = _effective({**role_args_raw[sp.role], **sp.args})
        name = f"{cfg.prefix}_{sp.role}_{sp.idx}"
        multi = len(sp.members) > 1
        members = [Member(rank, alias, cards, f"{name}_r{rank}" if multi else name)
                   for rank, (alias, cards) in enumerate(sp.members)]
        inst = Instance(sp.role, sp.idx, name, members, ports["port"], ports["bootstrap_port"],
                        ports["dist_init_port"], env, {}, args_raw, sp.env, sp.args)
        n = inst.total_cards
        inst.args = {k: (n if v == CARDS_TOKEN and n else v) for k, v in args_raw.items()}
        cfg.instances.append(inst)

    _check_topology(cfg, errors)
    _check_dist_init_ranges(cfg, errors)
    _check_parallel(cfg, errors)

    # 提示类告警
    for a in cfg.unused_nodes:
        warnings.append(f"节点 {a} 没有任何实例，不生成预检/上传")
    for i in cfg.prefills:
        if not i.args.get("enable-hierarchical-cache"):
            warnings.append(f"{i.name} 未启用 enable-hierarchical-cache，mooncake 分配将为 0（文档 FAQ 1）")
        if i.args.get("hicache-storage-backend") != "mooncake":
            warnings.append(f"{i.name} 的 hicache-storage-backend 不是 mooncake")
    if cfg.instances and not errors and not cfg.validated_scenario:
        cfg.scenario_warning = (f"当前场景（{describe_scenario(cfg)}）与文档验证场景（单节点 {VALIDATED_CARDS} 卡/实例）"
                                f"不同：env/args 中的调优参数未在该场景验证过，起不来时按 SKILL.md 排障表查 cookbook。")
        warnings.insert(0, cfg.scenario_warning)
    return cfg, errors, warnings


def _apply_instance_params(params: dict, specs: list, cfg: Config, errors: list):
    """params.instances 整段替换各实例的实例级 env / args（未列出的实例清空）。"""
    pinst = _params_table(params, "instances", errors)
    if pinst is None or not specs:
        return
    names = [f"{cfg.prefix}_{sp.role}_{sp.idx}" for sp in specs]
    for k in pinst:
        if k not in names:
            errors.append(f"params instances: 未知实例名 {k!r}（可用: {', '.join(names)}）")
    for sp, name in zip(specs, names):
        ent = pinst.get(name, {})
        if not isinstance(ent, dict):
            errors.append(f"params instances.{name} 必须是对象")
            continue
        for k in ent:
            if k not in ("env", "args"):
                errors.append(f"params instances.{name} 未知键 {k!r}（可用 env / args）")
        sp.env = _norm_kv(ent.get("env", {}), f"params instances.{name}.env", "env", errors)
        sp.args = _norm_kv(ent.get("args", {}), f"params instances.{name}.args", "args", errors)


def describe_scenario(cfg: Config) -> str:
    def part(insts, role):
        sizes = sorted({(i.total_cards, len(i.members)) for i in insts})
        desc = "/".join(f"{c} 卡" + (f"，跨 {k} 台" if k > 1 else "") for c, k in sizes)
        return f"{len(insts)} 个 {role}（每实例 {desc}）"
    return f"{cfg.scenario}：{part(cfg.prefills, 'prefill')}，{part(cfg.decodes, 'decode')}"


def _expand_specs(raw: dict, cfg: Config, errors: list) -> list:
    """[topology] 或显式 [[prefill]]/[[decode]] -> [Spec]。二选一。"""
    has_topo = "topology" in raw
    has_expl = any(r in raw for r in ROLES)
    if has_topo and has_expl:
        errors.append("[topology] 与 [[prefill]]/[[decode]] 只能二选一，不能同时出现")
        return []
    if has_topo:
        cfg.mode = "topology"
        return _expand_topology(raw["topology"], cfg, errors)
    cfg.mode = "explicit"
    if not has_expl:
        errors.append("缺少拓扑：请写 [topology]（场景简写）或 [[prefill]] + [[decode]]（显式实例）")
        return []
    return _explicit_specs(raw, cfg, errors)


def _expand_topology(top, cfg: Config, errors: list) -> list:
    if not isinstance(top, dict):
        errors.append("[topology] 必须是表")
        return []
    for k in top:
        if k not in TOPOLOGY_KEYS:
            errors.append(f"[topology] 未知键 {k!r}（可用 {', '.join(sorted(TOPOLOGY_KEYS))}）")
    ok = True
    counts, size = {}, {}
    for role in ROLES:
        v = top.get(role)
        if not is_int(v) or v < 1:
            errors.append(f"topology.{role} 必须是 >= 1 的整数（实例数），当前: {v!r}")
            ok = False
        counts[role] = v
        v = top.get(f"{role}_cards", 8)
        if not is_int(v) or v < 1:
            errors.append(f"topology.{role}_cards 必须是 >= 1 的整数，当前: {v!r}")
            ok = False
        size[role] = v
    per = top.get("cards_per_node", 8)
    if not is_int(per) or per < 1:
        errors.append(f"topology.cards_per_node 必须是 >= 1 的整数，当前: {per!r}")
        ok = False
    nodes = top.get("nodes")
    if nodes is None:
        nodes = list(cfg.nodes)
    elif not isinstance(nodes, list) or not nodes or not all(isinstance(x, str) for x in nodes):
        errors.append("topology.nodes 必须是非空的节点别名列表")
        ok = False
        nodes = []
    else:
        dup = sorted({x for x in nodes if nodes.count(x) > 1})
        if dup:
            errors.append(f"topology.nodes 有重复节点: {', '.join(dup)}")
            ok = False
        for x in nodes:
            if x not in cfg.nodes:
                errors.append(f"topology.nodes 中的 {x!r} 不在 [nodes] 中")
                ok = False
    if not ok:
        return []
    for role in ROLES:
        c = size[role]
        if c < per and per % c:
            errors.append(f"cards_per_node={per} 不能被 {role}_cards={c} 整除")
            ok = False
        elif c > per and c % per:
            errors.append(f"{role}_cards={c} 必须是 cards_per_node={per} 的整数倍（跨节点实例每台用满全部卡）")
            ok = False
    if not ok:
        return []

    # pack：先 prefill 后 decode，按节点顺序依次占卡
    placed = []          # (role, idx, [(节点序号, cards)])
    node_i, off = 0, 0
    for role in ROLES:
        c = size[role]
        for idx in range(1, counts[role] + 1):
            if c < per:
                if off + c > per:
                    node_i, off = node_i + 1, 0
                placed.append((role, idx, [(node_i, list(range(off, off + c)))]))
                off += c
            else:
                if off:
                    node_i, off = node_i + 1, 0
                k = c // per
                placed.append((role, idx, [(node_i + j, list(range(per))) for j in range(k)]))
                node_i += k
    need = node_i + (1 if off else 0)
    if need > len(nodes):
        errors.append(f"卡不够：{counts['prefill']}P{counts['decode']}D（prefill 每实例 {size['prefill']} 卡，"
                      f"decode 每实例 {size['decode']} 卡，每台 {per} 卡）需要 {need} 台节点，"
                      f"当前只有 {len(nodes)} 台")
        return []
    return [Spec(role, idx, [(nodes[ni], cards) for ni, cards in mem])
            for role, idx, mem in placed]


def _explicit_specs(raw: dict, cfg: Config, errors: list) -> list:
    specs = []
    for role in ROLES:
        arr = raw.get(role, [])
        if not isinstance(arr, list) or not all(isinstance(x, dict) for x in arr):
            errors.append(f"[[{role}]] 必须是表数组")
            arr = []
        if not arr:
            errors.append(f"至少需要 1 个 [[{role}]]")
        for idx, spec in enumerate(arr, 1):
            where = f"{role}_{idx}"
            node, nodes = spec.get("node"), spec.get("nodes")
            if node is not None and nodes is not None:
                errors.append(f"{where}: node 与 nodes 只能写一个")
                aliases = []
            elif nodes is not None:
                if not isinstance(nodes, list) or not nodes or not all(isinstance(x, str) for x in nodes):
                    errors.append(f"{where}: nodes 必须是非空的节点别名列表")
                    aliases = []
                else:
                    aliases = nodes
            elif isinstance(node, str):
                aliases = [node]
            else:
                errors.append(f"{where}: node（或 nodes）必填")
                aliases = []
            for a in aliases:
                if a not in cfg.nodes:
                    errors.append(f"{where}: node={a!r} 不在 [nodes] 中")
            aliases = aliases or [""]    # 保持至少一个 member，后续检查跳过未知节点
            if len(set(aliases)) != len(aliases):
                errors.append(f"{where}: nodes 有重复节点: {aliases!r}")
            cards = spec.get("cards")
            if not isinstance(cards, list) or not cards:
                errors.append(f"{where}: cards 必须是非空列表")
                cards = []
            elif not all(is_int(c) and c >= 0 for c in cards):
                errors.append(f"{where}: cards 必须是非负整数: {cards!r}")
                cards = []
            elif len(set(cards)) != len(cards):
                errors.append(f"{where}: cards 有重复: {cards!r}")
            ports = {k: spec[k] for k in ("port", "bootstrap_port", "dist_init_port") if k in spec}
            ienv = _norm_kv(spec.get("env", {}), f"{where}.env", "env", errors)
            iargs = _norm_kv(spec.get("args", {}), f"{where}.args", "args", errors)
            specs.append(Spec(role, idx, [(a, list(cards)) for a in aliases], ports, ienv, iargs))
    return specs


def _check_topology(cfg: Config, errors: list):
    """展开后按节点检查：卡不重叠；端口（只在 rank0 节点）互不重复，含 mooncake / router。"""
    cards: dict = {}
    ports: dict = {}
    for i in cfg.instances:
        for m in i.members:
            if m.node in cfg.nodes:
                for c in m.cards:
                    cards.setdefault(m.node, {}).setdefault(c, []).append(i.name)
        r0 = i.rank0
        if r0.node in cfg.nodes:
            for label, p in (("port", i.port), ("bootstrap_port", i.bootstrap_port),
                             ("dist_init_port", i.dist_init_port)):
                if p:
                    ports.setdefault(r0.node, {}).setdefault(p, []).append(f"{i.name}.{label}")
    if cfg.prefills and cfg.p1.rank0.node in cfg.nodes:
        d = ports.setdefault(cfg.p1.rank0.node, {})
        d.setdefault(cfg.rpc_port, []).append("mooncake.rpc_port")
        d.setdefault(cfg.metadata_port, []).append("mooncake.metadata_port")
        d.setdefault(cfg.router_port, []).append("router.port")
    for alias in cfg.nodes:
        for c, owners in sorted(cards.get(alias, {}).items()):
            if len(owners) > 1:
                errors.append(f"节点 {alias} 卡 {c} 被多个实例使用: {', '.join(owners)}")
        for p, owners in sorted(ports.get(alias, {}).items()):
            if len(owners) > 1:
                errors.append(f"节点 {alias} 端口 {p} 重复: {', '.join(owners)}")


def _check_dist_init_ranges(cfg: Config, errors: list):
    """dist-init 后的 sglang 内部端口也不能与另一实例的规划端口重叠。"""
    ranges = []
    ports = {}
    for i in cfg.instances:
        if i.rank0.node not in cfg.nodes:
            continue
        node = i.rank0.node
        lo = i.dist_init_port
        hi = lo + DIST_INIT_RESERVED - 1
        if hi > 65535:
            errors.append(f"{i.name}.dist_init_port={lo} 后续内部端口超出 65535（至少需要预留 {DIST_INIT_RESERVED} 个端口）")
        ranges.append((node, lo, hi, i.name))
        for label, port in (("port", i.port), ("bootstrap_port", i.bootstrap_port),
                            ("dist_init_port", i.dist_init_port)):
            ports.setdefault(node, []).append((port, f"{i.name}.{label}"))
    if cfg.prefills and cfg.p1.rank0.node in cfg.nodes:
        node = cfg.p1.rank0.node
        for label, port in (("mooncake.rpc_port", cfg.rpc_port),
                            ("mooncake.metadata_port", cfg.metadata_port),
                            ("router.port", cfg.router_port)):
            ports.setdefault(node, []).append((port, label))
    for n, (node, lo, hi, name) in enumerate(ranges):
        for other_node, other_lo, other_hi, other_name in ranges[n + 1:]:
            if node == other_node and max(lo, other_lo) <= min(hi, other_hi):
                errors.append(f"节点 {node} dist-init 预留区间重叠: {name} [{lo}, {hi}] / {other_name} [{other_lo}, {other_hi}]")
        for port, owner in ports.get(node, []):
            if lo <= port <= hi and owner != f"{name}.dist_init_port":
                errors.append(f"节点 {node} 的 {name} dist-init 预留区间 [{lo}, {hi}] 与 {owner}={port} 重叠")


def _check_parallel(cfg: Config, errors: list):
    for i in cfg.instances:
        n = i.total_cards
        if not n:
            continue
        if "tp-size" not in i.args:
            if n != 1:
                errors.append(f"{i.name}: 未设置 tp-size（默认 1），与总卡数={n} 不符")
        else:
            tp = to_int(i.args["tp-size"])
            if tp != n:
                errors.append(f"{i.name}: tp-size={i.args['tp-size']!r} 必须等于 len(cards)={n}")
        for k in ("dp", "ep", "attn-cp-size"):
            v = to_int(i.args.get(k))
            if v is not None and v > n:
                errors.append(f"{i.name}: {k}={v} 大于 len(cards)={n}")


# ───────────────────────── 渲染小工具 ─────────────────────────

def env_str(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def render_arg(k: str, v) -> str | None:
    """返回单个参数的文本（不含续行符），false -> None。"""
    if v is True:
        return f"--{k}"
    if v is False:
        return None
    if isinstance(v, list):
        return " ".join([f"--{k}"] + [q(str(x)) for x in v])
    return f"--{k} {q(str(v))}"


def derived_env(cfg: Config, inst: Instance, m: Member) -> list:
    node = cfg.node_of(m)
    hca = ",".join(f"{d}:{node.ib_port}" for d in node.ib_devices)
    out = [
        ("HIP_VISIBLE_DEVICES", ",".join(str(c) for c in m.cards)),
        ("NCCL_SOCKET_IFNAME", node.ifname),
        ("GLOO_SOCKET_IFNAME", node.ifname),
        ("NCCL_IB_HCA", hca),
        ("UCX_NET_DEVICES", hca),
        ("MC_ALLOWED_IBV_DEVICES", ",".join(node.ib_devices)),
        ("SGLANG_TORCH_PROFILER_DIR", f"{cfg.deploy_dir}/profile"),
    ]
    if inst.role == "prefill":
        p1ip = cfg.p1_node.ip
        out += [
            ("MOONCAKE_LOCAL_HOSTNAME", node.ip),
            ("MOONCAKE_TE_META_DATA_SERVER", f"http://{p1ip}:{cfg.metadata_port}/metadata"),
            ("MOONCAKE_MASTER", f"{p1ip}:{cfg.rpc_port}"),
            ("MOONCAKE_GLOBAL_SEGMENT_SIZE", cfg.segment_size),
        ]
    return out


def instance_env(cfg: Config, inst: Instance, m: Member) -> list:
    """[(K, 文本值)]：env.<role> ⊕ 实例 env，然后按 member 所在节点派生的项。"""
    items = [(k, env_str(v)) for k, v in inst.env.items()]
    return items + derived_env(cfg, inst, m)


def derived_args(cfg: Config, inst: Instance, m: Member) -> list:
    """[(参数名, 值)]：member 的派生参数，顺序即命令行顺序。"""
    node = cfg.node_of(m)
    out = [("host", "0.0.0.0"), ("port", inst.port), ("model-path", cfg.model_path)]
    if "speculative-algorithm" in inst.args:
        out.append(("speculative-draft-model-path", cfg.model_path))
    out.append(("served-model-name", cfg.served))
    if inst.multi:
        out += [("dist-init-addr", f"{cfg.node_of(inst.rank0).ip}:{inst.dist_init_port}"),
                ("nnodes", len(inst.members)), ("node-rank", m.rank)]
    else:
        out.append(("dist-init-addr", f"127.0.0.1:{inst.dist_init_port}"))
    return out + [
        ("deepep-config", cfg.deepep),
        ("disaggregation-mode", inst.role),
        ("disaggregation-bootstrap-port", inst.bootstrap_port),
        ("disaggregation-ib-device", ",".join(node.ib_devices)),
    ]


def instance_args(cfg: Config, inst: Instance, m: Member) -> list:
    """[参数文本]：派生项在前，然后 args.<role> ⊕ 实例 args（"@cards" 已替换）。"""
    out = [render_arg(k, v) for k, v in derived_args(cfg, inst, m)]
    for k, v in inst.args.items():
        s = render_arg(k, v)
        if s is not None:
            out.append(s)
    return out


def mooncake_args(cfg: Config) -> list:
    ip = cfg.p1_node.ip
    return [
        "--enable_http_metadata_server=true",
        f"--http_metadata_server_host={q(ip)}",
        f"--http_metadata_server_port={cfg.metadata_port}",
        f"--rpc_port={cfg.rpc_port}",
    ] + [q(a) for a in cfg.mc_extra]


def router_args(cfg: Config) -> list:
    out = ["--pd-disaggregation"]
    for p in cfg.prefills:
        out.append(f"--prefill http://{cfg.node_of(p.rank0).ip}:{p.port} {p.bootstrap_port}")
    for d in cfg.decodes:
        out.append(f"--decode http://{cfg.node_of(d.rank0).ip}:{d.port}")
    out += ["--host 0.0.0.0", f"--port {cfg.router_port}", f"--prefill-policy {q(cfg.prefill_policy)}"]
    return out + [q(a) for a in cfg.router_extra]


def worker_urls(cfg: Config) -> list:
    return [f"http://{cfg.node_of(i.rank0).ip}:{i.port}" for i in cfg.prefills + cfg.decodes]


def router_gate(cfg: Config) -> str:
    """router 脚本前置：所有 worker /health 200 才 exec router。"""
    t = cfg.startup_timeout_s
    # Requests routed before a worker is up hang in that worker's queue and need a full restart.
    # The gateway itself only registers a worker after its /health is 200, so this cannot deadlock.
    return f"""DEADLINE=$((SECONDS + {t}))
for u in {' '.join(worker_urls(cfg))}; do
  until [[ $(curl -s -o /dev/null -w '%{{http_code}}' -m 5 "$u/health") == 200 ]]; do
    if (( SECONDS >= DEADLINE )); then echo "router gate: $u/health not 200 after {t}s, router not started"; exit 1; fi
    sleep 5
  done
  echo "router gate: $u healthy"
done
"""


def launcher_body(env_items: list, cmd: str, args: list) -> str:
    """脚本里 cd 之后的部分：export 行 + exec 命令（每个参数一行）。"""
    lines = [f"export {k}={q(v)}" for k, v in env_items]
    exec_lines = [f"exec {cmd}"] + [f"  {a}" for a in args]
    lines.append(" \\\n".join(exec_lines))
    return "\n".join(lines) + "\n"


def launcher_script(cfg: Config, name: str, body: str) -> str:
    head = f"""#!/usr/bin/env bash
# generated by render.py from {cfg.src_name}; do not edit
set -euo pipefail
NAME={q(name)}
DEPLOY_DIR={q(cfg.deploy_dir)}
mkdir -p "$DEPLOY_DIR/logs" "$DEPLOY_DIR/run" "$DEPLOY_DIR/profile"
PID_FILE="$DEPLOY_DIR/run/$NAME.pid"
if [[ -f $PID_FILE ]] && kill -0 -- "-$(cat "$PID_FILE")" 2>/dev/null; then echo "$NAME already running" >&2; exit 1; fi
LOG="$DEPLOY_DIR/logs/${{NAME}}_$(date +%Y%m%d_%H%M%S).log"
ln -sfn "$LOG" "$DEPLOY_DIR/logs/$NAME.log"
echo $$ > "$PID_FILE"
cd "$DEPLOY_DIR"
exec >"$LOG" 2>&1
"""
    return head + body


def stop_script(cfg: Config) -> str:
    return f"""#!/usr/bin/env bash
# generated by render.py from {cfg.src_name}; do not edit
# usage: stop.sh <NAME>   stop the process group recorded in run/<NAME>.pid
set -uo pipefail
NAME=${{1:?usage: stop.sh NAME}}
DEPLOY_DIR={q(cfg.deploy_dir)}
PID_FILE="$DEPLOY_DIR/run/$NAME.pid"
PGID=$(cat "$PID_FILE" 2>/dev/null || true)
# pgid must be a number > 1 (never signal -1 / 0)
if [[ ! $PGID =~ ^[0-9]+$ ]] || (( PGID <= 1 )) || ! kill -0 -- "-$PGID" 2>/dev/null; then
  echo "$NAME not running"
  rm -f "$PID_FILE"
  exit 0
fi
kill -TERM -- "-$PGID" 2>/dev/null || true
for _ in $(seq 1 30); do
  kill -0 -- "-$PGID" 2>/dev/null || break
  sleep 1
done
if kill -0 -- "-$PGID" 2>/dev/null; then
  kill -KILL -- "-$PGID" 2>/dev/null || true
  sleep 1
fi
rm -f "$PID_FILE"
echo "$NAME stopped"
"""


# ───────────────────────── 预检 / 诊断脚本 ─────────────────────────

COMMON_LIB = r"""FAIL=0
WARN=0
emit() {
  local lvl=$1 item=$2
  shift 2
  case $lvl in
    FAIL) FAIL=$((FAIL+1)) ;;
    WARN) WARN=$((WARN+1)) ;;
  esac
  printf 'CHECK %s %s %s\n' "$lvl" "$item" "$*"
}
info_lines() {
  local l
  while IFS= read -r l; do
    [[ -n $l ]] && emit INFO "$1" "$l"
  done
  return 0
}
have() { command -v "$1" >/dev/null 2>&1; }
to() {
  local t=$1
  shift
  if have timeout; then timeout "$t" "$@"; else "$@"; fi
}
"""

PREFLIGHT_LIB = COMMON_LIB + r"""check_container() {
  local c=$1 item="container:$1" st img nm priv mounts bad=""
  if [[ $DOCKER_OK != 1 ]]; then
    emit WARN "$item" "skipped (docker unavailable)"
    return
  fi
  if ! to 20 docker container inspect "$c" >/dev/null 2>&1; then
    emit OK "$item" "not exist, will create"
    return
  fi
  st=$(docker container inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  emit INFO "$item" "exists, status=$st"
  if [[ -n $IMG_ID ]]; then
    img=$(docker container inspect -f '{{.Image}}' "$c" 2>/dev/null)
    [[ $img == "$IMG_ID" ]] || bad="$bad image=$img(expect $IMG_ID);"
  else
    emit WARN "$item" "image not present locally, skip image id comparison"
  fi
  mounts=$(docker container inspect -f '{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}}{{"\n"}}{{end}}' "$c" 2>/dev/null)
  printf '%s\n' "$mounts" | grep -Fxq -- "/opt/hyhal:/opt/hyhal:false" || bad="$bad mount(/opt/hyhal ro) missing;"
  printf '%s\n' "$mounts" | grep -Fxq -- "$WORK_DIR:$WORK_DIR:true" || bad="$bad mount($WORK_DIR rw) missing;"
  printf '%s\n' "$mounts" | grep -Fxq -- "$MODEL_ROOT:/model:false" || bad="$bad mount($MODEL_ROOT->/model ro) missing;"
  nm=$(docker container inspect -f '{{.HostConfig.NetworkMode}}' "$c" 2>/dev/null)
  [[ $nm == host ]] || bad="$bad NetworkMode=$nm(expect host);"
  priv=$(docker container inspect -f '{{.HostConfig.Privileged}}' "$c" 2>/dev/null)
  [[ $priv == true ]] || bad="$bad Privileged=$priv(expect true);"
  case $ON_CONFLICT in
    reuse)
      if [[ -n $bad ]]; then
        emit FAIL "$item" "config mismatch:$bad"
      elif [[ $st == running ]]; then
        emit OK "$item" "config matches, will reuse"
      elif [[ $st == exited || $st == created ]]; then
        emit OK "$item" "config matches, status=$st, will docker start"
      else
        emit FAIL "$item" "status=$st cannot be reused"
      fi
      ;;
    abort)
      emit FAIL "$item" "exists and on_conflict=abort${bad:+; mismatch:$bad}"
      ;;
    recreate)
      emit WARN "$item" "exists, will be removed and recreated (on_conflict=recreate)${bad:+; mismatch:$bad}"
      ;;
  esac
}
TCP_FILES="/proc/net/tcp /proc/net/tcp6"
proc_listen_line() {
  local hex f sl la ra st rest
  printf -v hex '%04X' "$1"
  for f in $TCP_FILES; do
    [[ -r $f ]] || continue
    while read -r sl la ra st rest; do
      if [[ $st == 0A && ${la##*:} == "$hex" ]]; then
        printf '%s local=%s\n' "${f##*/}" "$la"
        return 0
      fi
    done <"$f"
  done
  return 1
}
check_port() {
  local p=$1 label=$2 line=""
  case $PORT_MODE in
    ss)
      if [[ -n $(ss -ltnH "sport = :$p" 2>/dev/null) ]]; then
        line=$(ss -ltnpH "sport = :$p" 2>/dev/null | head -n 1)
      fi
      ;;
    proc)
      line=$(proc_listen_line "$p")
      ;;
    *)
      return 0
      ;;
  esac
  if [[ -n $line ]]; then
    emit FAIL "port:$p" "$label already in use: $line"
  else
    emit OK "port:$p" "$label free"
  fi
}
"""

DIAG_LIB = COMMON_LIB + r"""check_ib() {
  local dev=$1 port=$2 base st
  base=/sys/class/infiniband/$dev
  if [[ ! -d $base ]]; then
    emit FAIL "ib:$dev" "not found in /sys/class/infiniband"
    return
  fi
  st=$(cat "$base/ports/$port/state" 2>/dev/null)
  if [[ $st == *ACTIVE* ]]; then
    emit OK "ib:$dev" "port $port $st"
  else
    emit FAIL "ib:$dev" "port $port state='${st:-unreadable}' (not ACTIVE)"
  fi
}
show_gid() {
  local dev=$1 port=$2 idx=$3 users=$4 d g t
  d=/sys/class/infiniband/$dev/ports/$port
  g=$(cat "$d/gids/$idx" 2>/dev/null || echo '?')
  t=$(cat "$d/gid_attrs/types/$idx" 2>/dev/null || echo '?')
  emit INFO "gid:$idx" "ROCSHMEM_IB_GID_INDEX=$idx used by $users; $dev port $port gid=$g type=$t"
}
list_roce_v2() {
  local dev=$1 port=$2 d i g t found=""
  d=/sys/class/infiniband/$dev/ports/$port
  for i in $(ls "$d/gids" 2>/dev/null | sort -n); do
    g=$(cat "$d/gids/$i" 2>/dev/null)
    t=$(cat "$d/gid_attrs/types/$i" 2>/dev/null)
    if [[ $g == 0000:0000:0000:0000:0000:ffff:* && $t == *v2* ]]; then
      found="$found $i"
    fi
  done
  emit INFO "gid:roce-v2-ipv4" "$dev port $port indexes:${found:- none}"
}
"""


def preflight_script(cfg: Config, node: Node) -> str:
    """宿主机只读预检（DESIGN §6.1）：docker / 镜像 / 容器 / 端口 / 模型 / work_dir。"""
    mems = cfg.members_on(node.alias)           # [(实例, member)]
    p1_here = cfg.p1.rank0.node == node.alias
    L = [
        "#!/usr/bin/env bash",
        f"# generated by render.py from {cfg.src_name}; do not edit",
        f"# read-only preflight for node {node.alias}; prints CHECK lines and a SUMMARY line",
        f"IMAGE={q(cfg.image_ref)}",
        f"POLICY={q(cfg.pull_policy)}",
        f"ON_CONFLICT={q(cfg.on_conflict)}",
        f"WORK_DIR={q(cfg.work_dir)}",
        f"MODEL_ROOT={q(cfg.model_root)}",
        PREFLIGHT_LIB.rstrip("\n"),
        "main() {",
        "# 1. docker",
        "DOCKER_OK=0",
        "if ! have docker; then",
        '  emit FAIL docker "docker command not found"',
        "elif out=$(to 20 docker info --format '{{.ServerVersion}}' 2>&1); then",
        '  emit OK docker "server version $out"',
        "  DOCKER_OK=1",
        "  if storage=$(to 20 docker info --format 'driver={{.Driver}} root={{.DockerRootDir}} status={{json .DriverStatus}}' 2>&1); then",
        '    emit INFO docker-storage "$storage"',
        "    IMG_STORE=${storage#*root=}",
        "    IMG_STORE=${IMG_STORE%% status=*}",
        '    case "$storage" in',
        '      driver=containerd*|*io.containerd.snapshotter*|*"containerd image store"*)',
        "        # containerd image store keeps layers under containerd's root, not Docker data-root",
        "        # shortcut: reads only /etc/containerd/config.toml (containerd.io service), not a dockerd-managed containerd",
        r"""        IMG_STORE=$(awk -F"[\"']" '/^\[/{exit} /^[[:space:]]*root[[:space:]]*=/{print $2; exit}' /etc/containerd/config.toml 2>/dev/null)""",
        "        IMG_STORE=${IMG_STORE:-/var/lib/containerd} ;;",
        "    esac",
        "    # shortcut: system disk = same filesystem as /, a separate /var partition on the system disk passes",
        """    STORE_MNT=$(df -P "$IMG_STORE" 2>/dev/null | awk 'NR==2 {print $6}')""",
        "    if [[ -z $STORE_MNT ]]; then",
        '      emit WARN docker-storage "cannot resolve filesystem of image store $IMG_STORE"',
        "    elif [[ $STORE_MNT == / ]]; then",
        '      emit FAIL docker-storage "image store $IMG_STORE is on the system disk (/); move images to a data disk (set data-root, disable containerd-snapshotter)"',
        "    else",
        '      emit OK docker-storage "image store $IMG_STORE on $STORE_MNT"',
        "    fi",
        "  else",
        '    emit WARN docker-storage "docker info storage fields unavailable: $(printf \'%s\' "$storage" | head -n 1)"',
        "  fi",
        "else",
        '  emit FAIL docker "docker info failed: $(printf \'%s\' "$out" | head -n 1)"',
        "fi",
        "# 2. image",
        'IMG_ID=""',
        "if [[ $DOCKER_OK == 1 ]]; then",
        '  if IMG_ID=$(to 20 docker image inspect "$IMAGE" -f \'{{.Id}}\' 2>/dev/null); then',
        "    if [[ $POLICY == always ]]; then",
        '      emit WARN image "$IMAGE present ($IMG_ID) but pull_policy=always, will re-pull"',
        "    else",
        '      emit OK image "$IMAGE present ($IMG_ID)"',
        "    fi",
        "  else",
        '    IMG_ID=""',
        "    if [[ $POLICY == never ]]; then",
        '      emit FAIL image "$IMAGE missing and pull_policy=never"',
        "    else",
        '      emit WARN image "$IMAGE missing, will pull"',
        "    fi",
        "  fi",
        "else",
        '  emit WARN image "skipped (docker unavailable)"',
        "fi",
        "# 3. containers",
    ]
    L += [f"check_container {q(i.name)}" for i, m in mems]
    L += [
        "# 4. ports (ss, else /proc/net/tcp{,6} LISTEN state 0A)",
        "PORT_MODE=none",
        "if have ss; then",
        "  PORT_MODE=ss",
        "else",
        "  for f in $TCP_FILES; do",
        "    [[ -r $f ]] && PORT_MODE=proc",
        "  done",
        '  if [[ $PORT_MODE == proc ]]; then',
        '    emit INFO ports "ss not found, parsing /proc/net/tcp{,6}"',
        "  else",
        '    emit WARN ports "neither ss nor /proc/net/tcp available, port check skipped"',
        "  fi",
        "fi",
    ]
    for i, m in mems:
        if m.rank != 0:      # HTTP / bootstrap / dist-init 只在 rank0 节点监听
            continue
        for label, p in (("port", i.port), ("bootstrap_port", i.bootstrap_port),
                         ("dist_init_port", i.dist_init_port)):
            L.append(f"check_port {p} {q(i.name + '.' + label)}")
    if p1_here:
        L += [
            f"check_port {cfg.rpc_port} mooncake.rpc_port",
            f"check_port {cfg.metadata_port} mooncake.metadata_port",
            f"check_port {cfg.router_port} router.port",
        ]
    model_dir = f"{cfg.model_root}/{cfg.model_dir}"
    L += [
        "# 5. model",
        f"M={q(model_dir)}",
        'if [[ -f $M/config.json ]]; then',
        '  emit OK model "$M/config.json exists"',
        "else",
        '  emit FAIL model "$M/config.json not found"',
        "fi",
        "# 6. work_dir",
        "if [[ ! -d $WORK_DIR ]]; then",
        '  emit FAIL work_dir "$WORK_DIR does not exist"',
        "elif [[ ! -w $WORK_DIR ]]; then",
        '  emit FAIL work_dir "$WORK_DIR is not writable by current user"',
        "else",
        '  emit OK work_dir "$WORK_DIR exists and is writable"',
        "fi",
    ]
    if cfg.deepep_custom:
        if cfg.deepep_custom.startswith(cfg.work_dir.rstrip("/") + "/"):
            L += [
                f"if [[ -f {q(cfg.deepep_custom)} ]]; then",
                f"  emit OK deepep_config {q(cfg.deepep_custom + ' exists')}",
                "else",
                f"  emit FAIL deepep_config {q(cfg.deepep_custom + ' not found')}",
                "fi",
            ]
        else:
            L.append(f"emit INFO deepep_config {q(cfg.deepep_custom + ' is outside work_dir, not checked')}")
    L += [
        "}",
        "main </dev/null",
        'printf \'SUMMARY fail=%d warn=%d\\n\' "$FAIL" "$WARN"',
    ]
    return "\n".join(L) + "\n"


def diag_script(cfg: Config, node: Node) -> str:
    """宿主机只读诊断（DESIGN §6.2）：卡 / 网卡 / IB / GID / 模型文件数 / curl / free，按需执行。"""
    mems = cfg.members_on(node.alias)
    L = [
        "#!/usr/bin/env bash",
        f"# generated by render.py from {cfg.src_name}; do not edit",
        f"# read-only diagnostics for node {node.alias}; prints CHECK lines and a SUMMARY line",
        f"NODE_IP={q(node.ip)}",
        f"IFNAME={q(node.ifname)}",
        'if [[ -r /opt/hyhal/env.sh ]]; then source /opt/hyhal/env.sh; fi',
        DIAG_LIB.rstrip("\n"),
        "main() {",
    ]
    planned = " ".join(f"{m.name}={','.join(str(c) for c in m.cards)}" for i, m in mems)
    L += [
        "# 1. cards",
        "if have hy-smi; then",
        "  to 30 hy-smi 2>&1 | info_lines cards",
        "elif have rocm-smi; then",
        "  to 30 rocm-smi 2>&1 | info_lines cards",
        "else",
        '  emit WARN cards "neither hy-smi nor rocm-smi found"',
        "fi",
        f"emit INFO cards {q('planned: ' + planned)}",
        "# 2. network",
        "if have ip; then",
        '  nic=$(ip -o -4 addr show dev "$IFNAME" 2>/dev/null)',
        '  if [[ -n $nic ]]; then',
        '    emit OK "nic:$IFNAME" "$(printf \'%s\\n\' "$nic" | awk \'{print $4}\' | tr \'\\n\' \' \')"',
        "  else",
        '    emit FAIL "nic:$IFNAME" "interface not found or has no IPv4 address"',
        "  fi",
        '  if ip -o -4 addr show 2>/dev/null | awk \'{print $4}\' | cut -d/ -f1 | grep -Fxq -- "$NODE_IP"; then',
        '    emit OK node-ip "$NODE_IP is on a local interface"',
        "  else",
        '    emit WARN node-ip "$NODE_IP not found on any local interface"',
        "  fi",
        "else",
        '  emit WARN nic "ip command not found, interface checks skipped"',
        "fi",
    ]
    L += [f"check_ib {q(d)} {node.ib_port}" for d in node.ib_devices]
    dev0 = node.ib_devices[0]
    gid_users: dict = {}
    for i, m in mems:
        if "ROCSHMEM_IB_GID_INDEX" in i.env:
            gid_users.setdefault(env_str(i.env["ROCSHMEM_IB_GID_INDEX"]), []).append(m.name)
    L.append("# 3. gid")
    if not gid_users:
        L.append('emit INFO gid "no instance sets ROCSHMEM_IB_GID_INDEX"')
    for idx, users in gid_users.items():
        if idx.isdigit():
            L.append(f"show_gid {q(dev0)} {node.ib_port} {idx} {q(','.join(users))}")
        else:
            L.append(f"emit FAIL gid:{idx} {q('ROCSHMEM_IB_GID_INDEX is not a number, used by ' + ','.join(users))}")
    L.append(f"list_roce_v2 {q(dev0)} {node.ib_port}")
    L += [
        "# 4. model files",
        f"M={q(cfg.model_root + '/' + cfg.model_dir)}",
        "shopt -s nullglob",
        'files=("$M"/*.safetensors)',
        'emit INFO model-files "${#files[@]} safetensors files in $M"',
        "# 5. tools / memory",
        'if have curl; then emit OK curl "curl available"; else emit WARN curl "curl not found on host"; fi',
        'if have free; then free -g 2>&1 | info_lines mem; else emit WARN mem "free not found"; fi',
        "}",
        "main </dev/null",
        'printf \'SUMMARY fail=%d warn=%d\\n\' "$FAIL" "$WARN"',
    ]
    return "\n".join(L) + "\n"

# ───────────────────────── 卡空闲检查 ─────────────────────────

CARDS_LIB = r"""# Parsing assumptions (hy-smi / rocm-smi output formats were NOT verified on real hardware):
#  * memory, per tool, tried in this order until every planned card has a value:
#      1. `<tool> --showmeminfo vram --json`: JSON with one object per card, keyed "card<N>" (GPU<N> / DCU[<N>]
#         also accepted), holding "VRAM Total Memory (B)" and "VRAM Total Used Memory (B)" in bytes.
#      2. `<tool>` default table: a header line with a VRAM% column; data rows start with the card index and
#         have exactly as many fields as the header.
#      3. `<tool> --showmemuse`: lines like "GPU[<N>] : GPU memory use (%): <num>".
#  * processes: `<tool> --showpids` exiting 0. "none" = a line starting with "No " that mentions PID or process
#    (e.g. "No KFD PIDs currently running"); "some" = rows whose first field is a PID (second field = name).
#    Rows are mapped to cards with `<tool> --showpidgpus` ("PID <pid> is using <k> DRM device(s):" followed by
#    a line of card indexes). A process that cannot be mapped to a card never makes a card FREE (UNKNOWN).
#  * the card index printed by the tool is assumed to equal the index used in HIP_VISIBLE_DEVICES.
#  * BUSY = a process on the card, or memory used % > BUSY_PCT. FREE needs memory parsed and <= BUSY_PCT AND an
#    explicit "no processes". Everything else is UNKNOWN, with the first raw output lines in the detail.
have() { command -v "$1" >/dev/null 2>&1; }
to() {
  local t=$1
  shift
  if have timeout; then timeout "$t" "$@"; else "$@"; fi
}
AWK_JSON='
function num(s) { sub(/^[^:]*:[ \t]*"?/, "", s); sub(/[^0-9].*$/, "", s); return s }
{
  n = split($0, P, /[{},]/)
  for (k = 1; k <= n; k++) {
    s = P[k]; l = tolower(s)
    if (match(s, /"[A-Za-z]+\[?[0-9]+\]?"/)) { cur = substr(s, RSTART, RLENGTH); gsub(/[^0-9]/, "", cur) }
    else if (cur != "" && l ~ /vram total used memory/) used[cur] = num(s)
    else if (cur != "" && l ~ /vram total memory/) tot[cur] = num(s)
  }
}
END {
  for (c in tot)
    if (tot[c] ~ /^[0-9]+$/ && tot[c] + 0 > 0 && used[c] ~ /^[0-9]+$/)
      printf "M %s %.2f %sB/%sB\n", c, used[c] * 100 / tot[c], used[c], tot[c]
}'
AWK_TABLE='
!hdr && index($0, "VRAM%") {
  for (k = 1; k <= NF; k++) if ($k == "VRAM%") col = k
  if (col) hdr = NF
  next
}
hdr && NF == hdr && $1 ~ /^[0-9]+$/ && $col ~ /^[0-9]+(\.[0-9]+)?%?$/ && !seen[$1]++ {
  v = $col; sub(/%/, "", v); printf "M %s %s VRAM%% column\n", $1, v
}'
AWK_USE='
tolower($0) ~ /memory use|memory allocated|vram%/ && match($0, /\[[0-9]+\]/) {
  id = substr($0, RSTART + 1, RLENGTH - 2)
  v = $0; sub(/^.*:[ \t]*/, "", v); sub(/[ \t%]+$/, "", v)
  if (v ~ /^[0-9]+(\.[0-9]+)?$/ && !seen[id]++) printf "M %s %s memuse\n", id, v
}'
AWK_PROCS='
tolower($0) ~ /^[ \t]*no[ \t].*(pid|process)/ { none = 1; next }
$1 ~ /^[0-9]+$/ && NF >= 2 && $1 + 0 > 0 { print "P", $1, $2; n++ }
END { print "S", (n > 0 ? "some" : (none ? "none" : "unknown")) }'
AWK_PIDGPUS='
tolower($0) ~ /^[ \t]*pid[ \t]+[0-9]+[ \t]+is using/ { pid = $2; on = 1; next }
on && $0 ~ /^[ \t]*[0-9]+([ \t]+[0-9]+)*[ \t]*$/ { for (k = 1; k <= NF; k++) print "A", $k, pid; next }
{ on = 0 }'
MEMTXT=""
MEM_RAW=""
PROC_STATE=unknown
PROCTXT=""
ATTRTXT=""
PROC_RAW=""
BUSY=0
UNK=0
pick() { printf '%s\n' "$1" | awk -v t="$2" -v i="$3" '$1 == t && $2 == i { $1 = ""; $2 = ""; sub(/^ +/, ""); print; exit }'; }
snip() {
  printf '%s\n' "$1" | awk '{ gsub("\r", ""); gsub(/[ \t]+/, " ") }
    $0 !~ /^ ?$/ && n < 3 { s = s (n ? " | " : "") $0; n++ }
    END { print substr(s, 1, 240) }'
}
over() { awk -v p="$1" -v t="$2" 'BEGIN { exit !(p + 0 > t + 0) }'; }
card() { printf 'CARD %s %s %s\n' "$1" "$2" "$3"; }
need_mem() {
  local c
  for c in $CARDS; do
    [[ -n $(pick "$MEMTXT" M "$c") ]] || return 0
  done
  return 1
}
try_mem() {
  local prog=$1 out rc found=""
  shift
  out=$(to 30 "$@" 2>&1 </dev/null)
  rc=$?
  if [[ $rc == 0 ]]; then found=$(printf '%s\n' "$out" | awk "$prog" 2>/dev/null); fi
  if [[ -n $found ]]; then
    MEMTXT="$MEMTXT$found"$'\n'
  else
    MEM_RAW="$MEM_RAW${MEM_RAW:+ ## }[$*: rc=$rc] $(snip "$out")"
  fi
}
collect_mem() {
  need_mem && try_mem "$AWK_JSON" "$1" --showmeminfo vram --json
  need_mem && try_mem "$AWK_TABLE" "$1"
  need_mem && try_mem "$AWK_USE" "$1" --showmemuse
}
collect_procs() {
  local out rc r="" st
  out=$(to 30 "$1" --showpids 2>&1 </dev/null)
  rc=$?
  if [[ $rc == 0 ]]; then r=$(printf '%s\n' "$out" | awk "$AWK_PROCS" 2>/dev/null); fi
  st=$(printf '%s\n' "$r" | awk '$1 == "S" { print $2; exit }')
  case $st in
    none|some) PROC_STATE=$st; PROCTXT=$r ;;
    *) PROC_RAW="$PROC_RAW${PROC_RAW:+ ## }[$1 --showpids: rc=$rc] $(snip "$out")" ;;
  esac
  if [[ $PROC_STATE == some ]]; then
    out=$(to 30 "$1" --showpidgpus 2>&1 </dev/null)
    rc=$?
    if [[ $rc == 0 ]]; then ATTRTXT=$(printf '%s\n' "$out" | awk "$AWK_PIDGPUS" 2>/dev/null); fi
  fi
}
main() {
  local c t m pct busy mnote pnote pp un n_un un_list procok why="" tools=""
  for t in hy-smi rocm-smi; do
    have "$t" && tools="$tools $t"
  done
  if [[ -z $tools ]]; then
    why="neither hy-smi nor rocm-smi found in PATH"
  elif ! have awk; then
    why="awk not found, cannot parse hy-smi / rocm-smi output"
  fi
  if [[ -n $why ]]; then
    for c in $CARDS; do
      card "$c" UNKNOWN "$why"
      UNK=$((UNK+1))
    done
    return
  fi
  for t in $tools; do
    need_mem && collect_mem "$t"
    [[ $PROC_STATE == unknown ]] && collect_procs "$t"
  done
  n_un=0
  un_list=""
  if [[ $PROC_STATE == some ]]; then
    un=$(printf '%s\n%s\n' "$PROCTXT" "$ATTRTXT" | awk '$1 == "P" { p[$2] = $3 } $1 == "A" { a[$3] = 1 }
      END { for (k in p) if (!(k in a)) { n++; s = s (s ? "," : "") k ":" p[k] } printf "%d %s\n", n + 0, s }')
    n_un=${un%% *}
    un_list=${un#* }
  fi
  for c in $CARDS; do
    busy=0
    procok=0
    m=$(pick "$MEMTXT" M "$c")
    if [[ -n $m ]]; then
      pct=${m%% *}
      mnote="mem=${pct}% (${m#* })"
      if over "$pct" "$BUSY_PCT"; then
        busy=1
        mnote="$mnote > ${BUSY_PCT}%"
      else
        mnote="$mnote <= ${BUSY_PCT}%"
      fi
    else
      mnote="mem=? not parsed: ${MEM_RAW:0:400}"
    fi
    pp=""
    case $PROC_STATE in
      none)
        procok=1
        pnote="procs=none"
        ;;
      some)
        pp=$(printf '%s\n%s\n' "$PROCTXT" "$ATTRTXT" | awk -v i="$c" '$1 == "P" { nm[$2] = $3 }
          $1 == "A" && $2 == i { s = s (s ? "," : "") $3 ":" nm[$3] } END { print s }')
        if [[ -n $pp ]]; then
          busy=1
          pnote="procs=$pp"
        elif [[ $n_un == 0 ]]; then
          procok=1
          pnote="procs=none"
        else
          pnote="procs=? ($n_un process(es) not mapped to a card: ${un_list:0:120})"
        fi
        ;;
      *)
        pnote="procs=? not parsed: ${PROC_RAW:0:400}"
        ;;
    esac
    if [[ $busy == 1 ]]; then
      card "$c" BUSY "$mnote; $pnote"
      BUSY=$((BUSY+1))
    elif [[ -n $m && $procok == 1 ]]; then
      card "$c" FREE "$mnote; $pnote"
    else
      card "$c" UNKNOWN "$mnote; $pnote"
      UNK=$((UNK+1))
    fi
  done
}
"""


def cards_script(cfg: Config, node: Node) -> str:
    """宿主机只读卡空闲检查（DESIGN §6.7）：只查本节点计划使用的卡（含跨节点 member）。"""
    cards = sorted({c for i, m in cfg.members_on(node.alias) for c in m.cards})
    return "\n".join([
        "#!/usr/bin/env bash",
        f"# generated by render.py from {cfg.src_name}; do not edit",
        f"# read-only card idleness check for node {node.alias}; prints CARD lines and a SUMMARY line",
        f"CARDS={q(' '.join(str(c) for c in cards))}",
        f"BUSY_PCT={cfg.card_busy_mem_pct}",
        'if [[ -r /opt/hyhal/env.sh ]]; then source /opt/hyhal/env.sh; fi',
        CARDS_LIB.rstrip("\n"),
        "main </dev/null",
        'printf \'SUMMARY busy=%d unknown=%d\\n\' "$BUSY" "$UNK"',
    ]) + "\n"



# ───────────────────────── plan.json ─────────────────────────

def dexec(c: str, inner: str, flags: str = "bash -c") -> str:
    return f"docker exec {q(c)} {flags} {q(inner)}"


def docker_run_tokens(cfg: Config, name: str) -> list:
    t = [
        "docker run -dit --network=host", f"--name={q(name)}", "--privileged",
        "--device=/dev/kfd --device=/dev/dri", "--ipc=host", f"--shm-size={q(cfg.shm_size)}",
        "--group-add video", "--cap-add=SYS_PTRACE", "--security-opt seccomp=unconfined",
        "-u root", "--ulimit stack=-1:-1 --ulimit memlock=-1:-1",
        "-v /opt/hyhal:/opt/hyhal:ro",
        f"-v {q(cfg.work_dir + ':' + cfg.work_dir)}",
        f"-v {q(cfg.model_root + ':/model:ro')}",
    ]
    t += [q(a) for a in cfg.extra_args]
    t += [q(cfg.image_ref), "/bin/bash"]
    return t


def script_paths(cfg: Config, name: str) -> dict:
    dd = cfg.deploy_dir
    return {
        "script": f"{dd}/scripts/{name}.sh",
        "log": f"{dd}/logs/{name}.log",
        "pid_file": f"{dd}/run/{name}.pid",
    }


def prepare_dirs_cmd(cfg: Config) -> str:
    dirs = [f"{cfg.deploy_dir}/{part}" for part in ("scripts", "logs", "run", "profile")]
    checks = " && ".join(f"test -d {q(d)} && test -w {q(d)}" for d in dirs)
    return "mkdir -p " + " ".join(q(d) for d in dirs) + " && " + checks + " && echo prepared"


def component_cmds(cfg: Config, container: str, name: str, ready_port: int | None = None) -> dict:
    p = script_paths(cfg, name)
    alive_inner = (f"p=$(cat {q(p['pid_file'])} 2>/dev/null) && "
                   f"kill -0 $p 2>/dev/null && echo alive || echo dead")
    # The HTTP port can listen before sglang has finished initialization.  Keep
    # readiness separate from listen checks so router startup never races model
    # initialization.
    ready_inner = (f"grep -Fq {q(SERVER_READY_MARKER)} {q(p['log'])} 2>/dev/null && "
                   "echo ready || echo loading")
    fatal_scan_inner = (f"grep -nE {q(ERROR_PATTERN)} {q(p['log'])} 2>/dev/null | "
                        f"grep -vF {q(KNOWN_NONFATAL_ERROR_PATTERN)} | tail -n 20")
    warning_scan_inner = (f"grep -nF {q(KNOWN_NONFATAL_ERROR_PATTERN)} {q(p['log'])} 2>/dev/null | "
                          "tail -n 20")
    return {
        "container": container,
        "script": p["script"], "log": p["log"], "pid_file": p["pid_file"],
        "launch_cmd": f"docker exec -d {q(container)} setsid bash {q(p['script'])}",
        "stop_cmd": f"docker exec {q(container)} bash {q(cfg.deploy_dir + '/scripts/stop.sh')} {q(name)}",
        "alive_cmd": dexec(container, alive_inner),
        "ready_cmd": dexec(container, ready_inner),
        "error_scan_cmd": dexec(container, fatal_scan_inner),
        "warning_scan_cmd": dexec(container, warning_scan_inner),
    }


# Prints the body, then "<http code> ok" or "<http code> failed=<any non-empty *fail* field>".
FLUSH_CHECK = r"""r=$(curl -s -X POST -m 30 -w '\n%{{http_code}}' {url}/flush_cache); echo "${{r%$'\n'*}}"
python - "${{r##*$'\n'}}" "${{r%$'\n'*}}" <<'PY'
import json, sys
code, body = sys.argv[1:]
try:
    d = json.loads(body)
except ValueError:
    d = None
fails = {{k: v for k, v in d.items() if "fail" in k.lower() and v}} if isinstance(d, dict) else {{}}
print(code, f"failed={{fails}}" if fails else ("ok" if code == "200" else "bad"))
PY"""


def health_cmd(container: str, ip: str, port: int) -> str:
    return dexec(container, f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 http://{ip}:{port}/health")


LISTEN_CHECK = (
    'miss=""; for p in {ports}; do '
    'if command -v ss >/dev/null 2>&1; then ss -ltnH "sport = :$p" | grep -q . || miss="$miss $p"; '
    'else hex=$(printf "%04X" "$p"); found=0; '
    'for f in /proc/net/tcp /proc/net/tcp6; do [ -r "$f" ] || continue; '
    'while read -r sl la ra st rest; do [ "$st" = 0A ] && [ "${{la##*:}}" = "$hex" ] && found=1; done < "$f"; done; '
    '[ "$found" = 1 ] || miss="$miss $p"; fi; done; '
    'if [ -z "$miss" ]; then echo listening; else echo "missing:$miss"; fi'
)


def upload_script_names(cfg: Config, node: Node) -> list:
    names = [m.name for i, m in cfg.members_on(node.alias)]
    if cfg.p1.rank0.node == node.alias:
        names += ["mooncake_master", "router"]
    names.append("stop")
    return names


def upload_list(cfg: Config, node: Node, out: Path) -> list:
    dd = cfg.deploy_dir
    scripts = out / "scripts"
    names = upload_script_names(cfg, node)
    ups = [{"local": str((scripts / f"{n}.sh").resolve()), "remote": f"{dd}/scripts/{n}.sh"} for n in names]
    if not cfg.deepep_custom:
        ups.append({"local": str((out / "deepep-config.json").resolve()),
                    "remote": f"{dd}/deepep-config.json"})
    return ups


def upload_check_cmd(cfg: Config, node: Node) -> str:
    """Validate uploaded shell scripts on the host before any launch_cmd."""
    files = [f"{cfg.deploy_dir}/scripts/{n}.sh" for n in upload_script_names(cfg, node)]
    checks = []
    for path in files:
        checks.append(f"test -f {q(path)}")
        checks.append(f"! grep -q $'\\r' {q(path)}")
        checks.append(f"bash -n {q(path)}")
    return " && ".join(checks) + " && echo uploads_ok"


def build_plan(cfg: Config, out: Path, preflights: dict, diags: dict, cards: dict) -> dict:
    dd = cfg.deploy_dir
    p1 = cfg.p1
    p1node = cfg.p1_node
    pull_log = f"/tmp/{cfg.prefix}_pull.log"
    plan = {
        "prefix": cfg.prefix, "deploy_dir": dd, "served_model_name": cfg.served,
        "image": {
            "ref": cfg.image_ref, "pull_policy": cfg.pull_policy,
            "check_cmd": f"docker image inspect {q(cfg.image_ref)} -f '{{{{.Id}}}}'",
            "pull_cmd": f"nohup docker pull {q(cfg.image_ref)} > {q(pull_log)} 2>&1 &",
            "pull_log": pull_log,
            "tools_check_cmd": (f"docker run --rm --network=host --entrypoint bash {q(cfg.image_ref)} "
                                f"-lc {q(TOOLS_CHECK + '; ' + MOONCAKE_TOOLS_CHECK)}"),
        },
        "nodes": [], "containers": [], "instances": [],
    }
    diag_b64 = {a: base64.b64encode(t.encode("utf-8")).decode("ascii") for a, t in diags.items()}
    cards_b64 = {a: base64.b64encode(t.encode("utf-8")).decode("ascii") for a, t in cards.items()}
    for node in cfg.used_nodes:
        b64 = base64.b64encode(preflights[node.alias].encode("utf-8")).decode("ascii")
        plan["nodes"].append({
            "alias": node.alias, "mcp_host": node.mcp_host, "ip": node.ip,
            "cards_check_cmd": f"echo {cards_b64[node.alias]} | base64 -d | bash",
            "preflight_cmd": f"echo {b64} | base64 -d | bash",
            "diag_cmd": f"echo {diag_b64[node.alias]} | base64 -d | bash",
            "prepare_dirs_cmd": prepare_dirs_cmd(cfg),
            "upload_check_cmd": upload_check_cmd(cfg, node),
            "containers": [i.name for i, m in cfg.members_on(node.alias)],
            "uploads": upload_list(cfg, node, out),
        })

    for i in cfg.instances:
        c = i.name
        members = []
        for m in i.members:
            node = cfg.node_of(m)
            tools = TOOLS_CHECK
            tools += f"; test -f {q(cfg.model_path + 'config.json')} && echo model_path_ok"
            if m is p1.rank0:
                tools += "; " + MOONCAKE_TOOLS_CHECK
            w = f"{dd}/logs/.w_{c}"
            write_inner = (f"test -d {q(dd + '/scripts')} && test -d {q(dd + '/logs')} && "
                           f"test -d {q(dd + '/run')} && touch {q(w)} && rm {q(w)} && echo writable")
            plan["containers"].append({
                "name": c, "node": node.alias, "mcp_host": node.mcp_host,
                "run_cmd": " ".join(docker_run_tokens(cfg, c)),
                "start_cmd": f"docker start {q(c)}",
                "rm_cmd": f"docker rm -f {q(c)}",
                "tools_check_cmd": dexec(c, tools, "bash -lc"),
                "write_check_cmd": dexec(c, write_inner),
            })
            cmds = component_cmds(cfg, c, m.name, i.port if m.rank == 0 else None)
            if m.rank != 0:  # node_rank >= 1 skips warmup and never logs the ready marker; rank0 ready covers it
                cmds["ready_cmd"] = None
            members.append({
                "rank": m.rank, "node": node.alias, "mcp_host": node.mcp_host, "ip": node.ip,
                "cards": m.cards, **cmds,
                "mooncake_ok_cmd": (dexec(c, f"grep -c 'Mooncake store setup successfully' {q(cmds['log'])}")
                                    if i.role == "prefill" else None),
                "mooncake_expected": len(m.cards) if i.role == "prefill" else None,
            })
        r0 = cfg.node_of(i.rank0)
        plan["instances"].append({
            "name": i.name, "role": i.role, "index": i.index,
            "mcp_host": r0.mcp_host, "ip": r0.ip,     # rank0（health 在此检查）
            "port": i.port, "bootstrap_port": i.bootstrap_port, "dist_init_port": i.dist_init_port,
            "health_cmd": health_cmd(i.name, r0.ip, i.port),
            "listen_check_cmd": dexec(i.name, LISTEN_CHECK.format(ports=str(i.port))),
            "ranks": i.total_cards,
            "members": members,
        })

    mc = component_cmds(cfg, p1.name, "mooncake_master")
    listen_inner = LISTEN_CHECK.format(ports=f"{cfg.rpc_port} {cfg.metadata_port}")
    plan["mooncake"] = {
        "container": mc["container"], "mcp_host": p1node.mcp_host,
        "launch_cmd": mc["launch_cmd"], "stop_cmd": mc["stop_cmd"], "log": mc["log"],
        "ports": [cfg.rpc_port, cfg.metadata_port],
        "listen_check_cmd": dexec(p1.name, listen_inner),
        "mem_cmd": dexec(p1.name, f"grep -o 'Mem Storage: [^|]*' {q(mc['log'])} | tail -n 1"),
        "expected_total": cfg.expected_total, "ranks": cfg.total_ranks,
    }
    rt = component_cmds(cfg, p1.name, "router")
    url = f"http://{p1node.ip}:{cfg.router_port}"
    plan["router"] = {
        "container": rt["container"], "mcp_host": p1node.mcp_host, "url": url,
        "port": cfg.router_port, "launch_cmd": rt["launch_cmd"], "stop_cmd": rt["stop_cmd"],
        "log": rt["log"], "alive_cmd": rt["alive_cmd"],
        "health_cmd": health_cmd(p1.name, p1node.ip, cfg.router_port),
        "flush_cache_cmd": (f"docker exec {q(p1.name)} bash -lc "
                            f"{q(f'curl -sS -X POST -m 30 {url}/flush_cache')}"),
        "flush_cache_check_cmd": dexec(p1.name, FLUSH_CHECK.format(url=url)),
        "expected_workers": len(cfg.instances),
        "workers_check_cmd": dexec(p1.name, (
            f"b=$(curl -s -m 5 {url}/workers; curl -s -m 5 {url}/list_workers); miss=; "
            f"for u in {' '.join(worker_urls(cfg))}; do "
            'grep -qF "$u" <<<"$b" || miss="$miss $u"; done; '
            '[[ -z $miss ]] && echo registered || echo "missing:$miss"')),
        "error_scan_cmd": rt["error_scan_cmd"],
        "warning_scan_cmd": rt["warning_scan_cmd"],
    }
    body = json.dumps({
        "model": cfg.served,
        "messages": [{"role": "user", "content": "Hello, introduce yourself in one sentence."}],
        "max_tokens": 128, "temperature": 0,
    }, separators=(",", ":"), ensure_ascii=False)
    curl_one = (f"curl -sS -m 600 {url}/v1/chat/completions "
                f"-H 'Content-Type: application/json' -d {q(body)}")
    plan["final_curl_cmd"] = dexec(p1.name, curl_one)
    plan["acceptance_request_count"] = max(1, len(cfg.prefills))
    check_py = ("import json,sys; d=json.load(sys.stdin); c=d.get('choices') or []; "
                "assert c, d; m=c[0].get('message') or {}; "
                "assert m.get('content') or m.get('reasoning_content'), d; print('response_ok')")
    plan["final_curl_validate_cmd"] = dexec(
        p1.name, f"{curl_one} | python -c {q(check_py)}")
    batch = (f"set -eu; n=1; while [ $n -le {plan['acceptance_request_count']} ]; do "
             f"printf 'PROBE %s\\n' \"$n\"; {curl_one}; printf '\\n'; n=$((n+1)); done")
    plan["final_curl_batch_cmd"] = dexec(p1.name, batch)
    checked_batch = (f"set -eu; n=1; while [ $n -le {plan['acceptance_request_count']} ]; do "
                     f"printf 'PROBE %s\\n' \"$n\"; {curl_one} | python -c {q(check_py)}; "
                     f"n=$((n+1)); done")
    plan["final_curl_validate_batch_cmd"] = dexec(p1.name, checked_batch)
    plan["stop_order"] = (["router"] + [i.name for i in cfg.decodes]
                          + [i.name for i in cfg.prefills] + ["mooncake"])
    return plan


# ───────────────────────── plan.md ─────────────────────────

def load_baseline():
    """返回 {role: (env, args)}（有效值）或 None。"""
    try:
        with open(BASELINE_TOML, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    out = {}
    for role in ROLES:
        e = {k: v for k, v in raw.get("env", {}).get(role, {}).items()}
        a = {k.lstrip("-"): v for k, v in raw.get("args", {}).get(role, {}).items()}
        out[role] = (_effective(e), _effective(a))
    return out


def fmt_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def diff_dict(base: dict, cur: dict, label: str) -> list:
    lines = []
    for k, v in cur.items():
        if k not in base:
            lines.append(f"- {label} 新增 `{k}` = {fmt_val(v)}")
        elif base[k] != cur[k] or type(base[k]) is not type(cur[k]):
            lines.append(f"- {label} 改值 `{k}`: {fmt_val(base[k])} → {fmt_val(v)}")
    for k, v in base.items():
        if k not in cur:
            lines.append(f"- {label} 删除 `{k}`（原值 {fmt_val(v)}）")
    return lines


def diff_section(cfg: Config) -> str:
    base = load_baseline()
    if base is None:
        return "（无法读取基线 deploy.example.toml，未比较）\n"
    parts = []
    for role in ROLES:
        be, ba = base[role]
        lines = diff_dict(be, cfg.role_env[role], "env") + diff_dict(ba, cfg.role_args[role], "args")
        if lines:
            parts.append(f"**角色 {role}**（`[env.{role}]` / `[args.{role}]` 相对默认值）\n\n" + "\n".join(lines))
    for i in cfg.instances:
        lines = (diff_dict(cfg.role_env[i.role], i.env, "env")
                 + diff_dict(cfg.role_args[i.role], i.args_raw, "args"))
        if lines:
            parts.append(f"**实例 {i.name}**（相对本角色配置）\n\n" + "\n".join(lines))
    return "\n\n".join(parts) + "\n" if parts else "无\n"


def plan_md(cfg: Config, out: Path, warnings: list, bodies: dict, plan: dict) -> str:
    p1 = cfg.p1
    p1node = cfg.p1_node
    dd = cfg.deploy_dir
    L = [f"# 部署计划：{cfg.prefix}", ""]
    if cfg.scenario_warning:
        L += [f"> **WARN**：{cfg.scenario_warning}", ""]
    L += [f"由 render.py 根据 `{cfg.src_name}` 生成。确认前远端不会执行任何写操作。", ""]
    if cfg.params_src:
        L += [params_source_line(cfg), ""]
    how = "`[topology]` 场景简写自动展开" if cfg.mode == "topology" else "显式 `[[prefill]]`/`[[decode]]`"
    L += ["## 1. 拓扑", "",
          f"场景：{describe_scenario(cfg)}；写法：{how}；端口自动分配基数 "
          f"http={cfg.http_base} / bootstrap={cfg.bootstrap_base} / dist-init={cfg.dist_base}"
          "（显式写死的端口除外）。",
          "",
          "| 实例 | rank | 节点 | 容器 | mcp_host | ip | 卡 | port | bootstrap | dist-init |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for i in cfg.instances:
        for m in i.members:
            n = cfg.node_of(m)
            ports = (f"{i.port} | {i.bootstrap_port} | {i.dist_init_port}" if m.rank == 0
                     else "- | - | -")
            L.append(f"| {i.name} | {m.rank} | {m.node} | {i.name} | {n.mcp_host} | {n.ip} "
                     f"| {','.join(map(str, m.cards))} | {ports} |")
    if any(i.multi for i in cfg.instances):
        L += ["", "跨节点实例：所有 rank 同时启动；HTTP / bootstrap / health 只在 rank0，"
                  "`--dist-init-addr` 指向 rank0 节点 ip；`@cards` = 实例总卡数。"]
    L += ["",
          "- 未使用节点：" + (", ".join(f"{a}（{cfg.nodes[a].mcp_host}）" for a in cfg.unused_nodes)
                            if cfg.unused_nodes else "无"),
          f"- mooncake master：容器 `{p1.name}`（rank0，{p1node.mcp_host} / {p1node.ip}），"
          f"rpc `{cfg.rpc_port}`，metadata `{cfg.metadata_port}`；"
          f"每 rank segment `{cfg.segment_size}`，prefill 共 {cfg.total_ranks} rank，"
          f"期望总量 `{cfg.expected_total}`",
          f"- router：容器 `{p1.name}`，端口 `{cfg.router_port}`，prefill 策略 `{cfg.prefill_policy}`",
          f"- 对外地址：`http://{p1node.ip}:{cfg.router_port}/v1`",
          f"- 模型名：`{cfg.served}`（模型目录 `{cfg.model_root}/{cfg.model_dir}`）",
          f"- 镜像：`{cfg.image_ref}`（pull_policy = `{cfg.pull_policy}`，on_conflict = `{cfg.on_conflict}`）",
          f"- 部署目录：`{dd}`", ""]
    others = [w for w in warnings if w != cfg.scenario_warning]
    if others:
        L += ["**告警**", ""] + [f"- {w}" for w in others] + [""]

    cards_by_node = "；".join(
        f"{n.alias}: {','.join(str(c) for c in sorted({c for i, m in cfg.members_on(n.alias) for c in m.cards}))}"
        for n in cfg.used_nodes)
    L += ["## 2. 将执行的动作", "",
          f"**先跑卡检查**：在各节点执行 plan.json `nodes[].cards_check_cmd`（只读；计划卡号 {cards_by_node}；"
          f"显存占用 > `card_busy_mem_pct` = {cfg.card_busy_mem_pct}% 或卡上有进程判 BUSY）。"
          "全部 FREE 才继续；有 BUSY 停下报告占用者，有 UNKNOWN 请用户确认。", "",
          f"1. 镜像（以预检结果为准）：各节点先 `docker image inspect`；缺失，或 pull_policy=always，"
          f"才执行 `docker pull`。节点：{', '.join(n.mcp_host for n in cfg.used_nodes)}。",
          f"2. 容器（on_conflict = `{cfg.on_conflict}`，已存在且一致则复用/start，不存在才 `docker run`）：", ""]
    for i in cfg.instances:
        for m in i.members:
            n = cfg.node_of(m)
            L += [f"在 `{n.mcp_host}` 上：", "", "```bash",
                  " \\\n  ".join(docker_run_tokens(cfg, i.name)), "```", ""]
    L += ["3. 目录与容器：先以宿主机用户执行每个节点的 `prepare_dirs_cmd`，确认 `scripts/`、`logs/`、`run/`、`profile/` 可写，再按预检结论执行 `docker run` / `docker start`；容器内不负责创建宿主机部署目录。", "",
           "4. 上传文件（每个节点各一份）：", "", "| 节点 | 本地 | 远端 |", "|---|---|---|"]
    for nd in plan["nodes"]:
        for u in nd["uploads"]:
            L.append(f"| {nd['mcp_host']} | `{u['local']}` | `{u['remote']}` |")
    L += ["",
          "5. 启动顺序：上传脚本后先把 params.md（各实例最终 env 与启动命令）交给用户确认；确认后先 mooncake master，再同时启动所有 prefill 和 decode members；轮询 alive_cmd、rank0 ready_cmd、端口监听和实例 /health，全部实例 /health 200 后再启动 router（router 脚本自身也会等所有 worker /health 200 才启动），router health 200 且 workers_check_cmd 输出 registered 后才验收。",
          "跨节点实例的所有 members 必须同时启动；ready marker 和 health 只查 rank0（rank>0 不打印 ready marker）。",
          "   跨节点实例的每个 member（每个节点的容器/脚本）同时启动；`/health` 只查 rank0。",
          "6. 验收：各实例 `/health`；prefill 每个 member 日志 `Mooncake store setup successfully` 行数 = 该 member 卡数；"
          f"mooncake `Mem Storage` = `{cfg.expected_total}`；运行 `final_curl_batch_cmd`（每个 prefill 一次请求），并结合 router 日志确认 round-robin 覆盖每个 prefill。", ""]

    L += param_sections(cfg, bodies, 3)
    return "\n".join(L)


def param_sections(cfg: Config, bodies: dict, first: int) -> list:
    """差异 + 每个 member 的最终 env / 启动命令 + mooncake / router 命令（plan.md 第 3、4 节 = params.md 第 1、2 节）。"""
    p1 = cfg.p1
    L = [f"## {first}. 与文档默认值的差异", "", diff_section(cfg).rstrip("\n"), "",
         f"## {first + 1}. 最终 env 与启动命令", ""]
    for i in cfg.instances:
        for m in i.members:
            title = (f"{m.name}（{i.name} rank {m.rank}，节点 {m.node}）" if i.multi else i.name)
            L += [f"### {title}", "", "```bash", bodies[m.name].rstrip("\n"), "```", ""]
    L += ["### mooncake_master（在 " + p1.name + " 容器）", "", "```bash",
          bodies["mooncake_master"].rstrip("\n"), "```", "",
          "### router（在 " + p1.name + " 容器）", "", "```bash",
          bodies["router"].rstrip("\n"), "```", ""]
    return L


def params_source_line(cfg: Config) -> str:
    return (f"> 参数来自 `{cfg.params_src}`（--params：其中的 env / args / instances / mooncake / router "
            f"覆盖 `{cfg.src_name}` 的对应项）。")


def params_json(cfg: Config) -> str:
    """params.json：可改的 env / args / instances / mooncake / router + 只读的 derived（已解析）。"""
    derived = {}
    for i in cfg.instances:
        for m in i.members:
            derived[m.name] = {"env": dict(derived_env(cfg, i, m)),
                               "args": dict(derived_args(cfg, i, m))}
    data = {
        "_readme": PARAMS_README,
        "env": {r: dict(cfg.role_env[r]) for r in ROLES},
        "args": {r: dict(cfg.role_args[r]) for r in ROLES},
        "instances": {i.name: {"env": dict(i.inst_env), "args": dict(i.inst_args)} for i in cfg.instances},
        "mooncake": {"segment_size": cfg.segment_size, "extra_args": list(cfg.mc_extra)},
        "router": {"prefill_policy": cfg.prefill_policy, "extra_args": list(cfg.router_extra)},
        "derived": derived,
    }
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


def params_md(cfg: Config, bodies: dict) -> str:
    how = (f"`{cfg.params_src}` 后带 `--params` 重新渲染" if cfg.params_src
           else f"`{cfg.src_name}` 后重新渲染")
    L = [f"# 启动参数确认：{cfg.prefix}", "",
         "> **启动前须用户确认**：下列参数与命令确认无误之前，不得执行任何 `launch_cmd`。"
         f"需要修改请改 {how}、重新上传，再确认一次。", ""]
    if cfg.params_src:
        L += [params_source_line(cfg), ""]
    if cfg.scenario_warning:
        L += [f"> **WARN**：{cfg.scenario_warning}", ""]
    return "\n".join(L + param_sections(cfg, bodies, 1))


# ───────────────────────── 生成 ─────────────────────────

def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def generate(cfg: Config, out: Path, warnings: list) -> Path:
    out = Path(out)
    bodies, scripts = {}, {}
    for i in cfg.instances:
        for m in i.members:
            bodies[m.name] = launcher_body(instance_env(cfg, i, m), SERVER_CMD, instance_args(cfg, i, m))
            scripts[m.name] = launcher_script(cfg, m.name, bodies[m.name])
    bodies["mooncake_master"] = launcher_body([], "mooncake_master", mooncake_args(cfg))
    scripts["mooncake_master"] = launcher_script(cfg, "mooncake_master", bodies["mooncake_master"])
    bodies["router"] = launcher_body([], ROUTER_CMD, router_args(cfg))
    scripts["router"] = launcher_script(cfg, "router", router_gate(cfg) + bodies["router"])
    scripts["stop"] = stop_script(cfg)
    preflights = {n.alias: preflight_script(cfg, n) for n in cfg.used_nodes}
    diags = {n.alias: diag_script(cfg, n) for n in cfg.used_nodes}
    cards = {n.alias: cards_script(cfg, n) for n in cfg.used_nodes}

    for name, text in scripts.items():
        write_text(out / "scripts" / f"{name}.sh", text)
    for alias, text in preflights.items():
        write_text(out / "preflight" / f"{alias}.sh", text)
    for alias, text in diags.items():
        write_text(out / "diag" / f"{alias}.sh", text)
    for alias, text in cards.items():
        write_text(out / "cards" / f"{alias}.sh", text)
    if not cfg.deepep_custom:
        write_text(out / "deepep-config.json", DEEPEP_DEFAULT.read_text(encoding="utf-8"))

    plan = build_plan(cfg, out, preflights, diags, cards)
    write_text(out / "plan.json", json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    md = out / "plan.md"
    write_text(md, plan_md(cfg, out, warnings, bodies, plan))
    write_text(out / "params.md", params_md(cfg, bodies))
    write_text(out / "params.json", params_json(cfg))
    return md


def _fix_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            if s.isatty():
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _fix_stdio()
    ap = argparse.ArgumentParser(
        description="deploy.toml -> scripts / preflight / plan.md / params.md / params.json / plan.json")
    ap.add_argument("toml", type=Path, help="deploy.toml")
    ap.add_argument("-o", "--out-dir", type=Path, default=None,
                    help="输出目录，默认 <deploy.toml 所在目录>/build/<prefix>")
    ap.add_argument("--params", type=Path, default=None, metavar="PARAMS_JSON",
                    help="params.json：覆盖 deploy.toml 的 env / args / instances / mooncake / router（整段替换）")
    ns = ap.parse_args(argv)

    cfg, errors, warnings = load(ns.toml, ns.params)
    if errors:
        print(f"校验失败，共 {len(errors)} 项错误：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2
    out = ns.out_dir or (ns.toml.resolve().parent / "build" / cfg.prefix)
    md = generate(cfg, out, warnings)
    print(f"plan.md: {md.resolve()}")
    print(f"params.md: {(out / 'params.md').resolve()}")
    print(f"params.json: {(out / 'params.json').resolve()}")
    for w in warnings:
        print(f"告警: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

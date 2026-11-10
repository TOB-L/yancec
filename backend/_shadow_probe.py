"""阴影探针：模块级已绑定的名字，被函数内的局部赋值**挡掉**，且存在够不到赋值点的读取。

## 病案（2026-09-17 实锤，`graph.py:node_deliver`）
`logger` 是模块级（`graph.py:35`）。`node_deliver` 体内出现了
    logger = __import__("logging").getLogger(__name__)
一共 **7 次**，全部写在 `except` 处理块里。Python 的作用域规则是"函数内只要有一处赋值，
这个名字在整个函数里就是局部名"——于是模块级那个 `logger` 在**整个 900 行函数里再也够不到**。
后果：函数里任何一处 `logger.X(...)`，只要执行路径上没有先经过那 7 处赋值，就是
`UnboundLocalError`。实际炸的是嵌套函数 `_comp_log`（它把 `logger` 当闭包自由变量读），
而它**只在别的东西已经失败时才被调用**——所以平时测不出来，一出事就是
"接漏的抹布自己破了"：本该"跳过这一项附录"，实际整章交付抛异常。

这 7 行不是一次写的：先有 1 处崩、就地补 1 行；再崩一处、再补 1 行。**根因一次没动。**

## 判据（取最窄、最准的一条）
  (1) 名字在**模块级**被绑定（简单赋值 / import）；
  (2) 同一文件某函数体内对该名字有**名字赋值**（⇒ 该函数内它是局部的，模块级被挡）；
  (3) 该函数内（含嵌套函数，那里它是闭包自由变量）存在**读取点**，
      且该读取点不被任何赋值点覆盖。
三条同时成立 → 报告。条件 (1) 把误报压到极低：**光是"局部变量可能未绑定"不算病，
"把模块级全局挡掉"才是**。

## 覆盖 = 保证块
一个赋值点覆盖某读取点，当且仅当：
  A. 赋值块文本上包住读取点且赋值行 ≤ 读取行；**或**
  B. 赋值位于一个"保证块"内，且该块整体在读取点之前结束——因为控制流若走到读取点，
     必然已经把该块执行完（否则异常会直接跳过读取点）。
保证块 = `with` 体 ／ 处理块全部以 return|raise|continue|break 结束的 `try` 体 ／
        body 与 orelse **都**赋了该名字的 `if-else`。

## 曾经踩过的三个坑（都写在这里，省得下次再踩）
  1. `Subscript`/`Attribute` 赋值（`_LAST[host] = now`）**不是名字绑定**，
     第一版用 `ast.walk(target)` 收名字，把全仓 6 处模块级字典缓存全误报了。
  2. `ast.walk` 是 BFS，`break` 拦不住它下钻嵌套作用域；换 `NodeVisitor` 后
     若不 override 复合语句，仍会 generic_visit 下钻 body。
  3. 函数**参数**本身在入口就绑定，不算"未赋值路径"。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {
    "venv", "__pycache__", "node_modules", "test_chroma", "models",
    "npm_cache", "pip_cache", ".veritmp", "_bundle_test",
}
TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _target_names(t: ast.AST) -> list[str]:
    """**名字**绑定目标。下标/属性赋值不是名字绑定——这是第一版最大的误报源。"""
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [n for e in t.elts for n in _target_names(e)]
    if isinstance(t, ast.Starred):
        return _target_names(t.value)
    return []


def _bind_direct(stmts: list[ast.stmt]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in stmts:
        names: list[str] = []
        if isinstance(s, ast.Assign):
            for t in s.targets:
                names += _target_names(t)
        elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
            names += _target_names(s.target)
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            names += [(a.asname or a.name).split(".")[0] for a in s.names]
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(s.name)
        elif isinstance(s, (ast.Global, ast.Nonlocal)):
            names += list(s.names)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            names += _target_names(s.target)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for it in s.items:
                if it.optional_vars is not None:
                    names += _target_names(it.optional_vars)
        elif isinstance(s, ast.Try):
            for h in s.handlers:
                if h.name:
                    names.append(h.name)
        for n in names:
            out.setdefault(n, s.lineno)
    return out


def _terminates(stmts: list[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], TERMINATORS)


def _assigned_somewhere(stmts: list[ast.stmt], name: str) -> bool:
    """名字在这串语句里（含任意深度）被**名字**绑定过"""
    for s in stmts:
        if name in _bind_direct([s]):
            return True
        for attr in ("body", "orelse", "finalbody"):
            v = getattr(s, attr, None)
            if isinstance(v, list) and v and isinstance(v[0], ast.stmt) and _assigned_somewhere(v, name):
                return True
        for h in getattr(s, "handlers", None) or []:
            if _assigned_somewhere(h.body, name):
                return True
    return False


def _guaranteed(owner: ast.stmt, name: str) -> bool:
    """`owner` 是不是一个"要么执行完、要么碰不到后面代码"的块"""
    if isinstance(owner, (ast.With, ast.AsyncWith)):
        return True
    if isinstance(owner, ast.Try):
        # ① 处理块全部终止（return/raise/continue/break）→ 走不到后面的读取点；
        # ② 或者 try 体**直接**赋了该名，且**每个**处理块也都直接赋了它
        #    ——`try: note="" except Exception as e: note=str(e)` 这种覆盖是穷尽的，
        #    漏掉 ② 会把最常见的那种写法误报成病（第一版就是这样）。
        if owner.handlers and all(_terminates(h.body) for h in owner.handlers):
            return True
        if name in _bind_direct(owner.body) and all(
            name in _bind_direct(h.body) for h in owner.handlers
        ):
            return True
        return False
    if isinstance(owner, ast.If):
        return bool(owner.orelse) and _assigned_somewhere(owner.body, name) and _assigned_somewhere(owner.orelse, name)
    return False


def module_bound(tree: ast.Module) -> set[str]:
    """模块级绑定（含 import）。**不下钻函数/类定义**——第一版 ast.walk 把函数内局部
    名字也算成模块级，于是 `result`/`v` 一类参数与临时名全被误报。"""
    names: set[str] = set()

    def collect(stmts: list[ast.stmt]) -> None:
        for s in stmts:
            names.update(_bind_direct([s]))
            if isinstance(s, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                attrs = ["body", "orelse", "finalbody"]
                for attr in attrs:
                    v = getattr(s, attr, None)
                    if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                        collect(v)
                for h in getattr(s, "handlers", None) or []:
                    collect(h.body)

    collect([s for s in tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))])
    return names


class ShadowScan:
    def __init__(self, path: Path, mod_names: set[str]) -> None:
        self.path = path
        self.mod = mod_names
        self.hits: list[tuple[int, str, str, list[int], int]] = []

    def run(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._check(node)

    def _check(self, fn) -> None:
        # 参数在函数入口即绑定 → 不算未赋值路径
        params: set[str] = set()
        a = fn.args
        for grp in (a.posonlyargs, a.args, a.kwonlyargs):
            params |= {x.arg for x in grp}
        if a.vararg:
            params.add(a.vararg.arg)
        if a.kwarg:
            params.add(a.kwarg.arg)

        assigns: dict[str, list[tuple[int, ast.stmt, int]]] = {}  # 名 → [(块id, owner_stmt, 行)]
        reads: dict[str, list[tuple[set[int], int]]] = {}

        class _R(ast.NodeVisitor):
            def __init__(self, anc, cur):
                self.anc, self.cur = anc, cur

            def visit_Name(self, n):
                if isinstance(n.ctx, ast.Load):
                    reads.setdefault(n.id, []).append((set(self.anc) | {self.cur}, n.lineno))

            def _head_only(self, n):
                for f in n._fields:
                    v = getattr(n, f, None)
                    if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                        continue
                    if isinstance(v, ast.AST):
                        self.visit(v)
                    elif isinstance(v, list):
                        for e in v:
                            if isinstance(e, ast.AST) and not isinstance(
                                e, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
                            ):
                                self.visit(e)

            visit_If = _head_only
            visit_For = _head_only
            visit_AsyncFor = _head_only
            visit_While = _head_only
            visit_With = _head_only
            visit_AsyncWith = _head_only
            visit_Try = _head_only
            visit_TryStar = _head_only
            visit_ExceptHandler = _head_only
            visit_Match = _head_only

        def walk(stmts: list[ast.stmt], anc: tuple[int, ...], owner: ast.stmt | None, depth: int) -> None:
            for s in stmts:
                for n in _bind_direct([s]):
                    # owner 必须是**外层复合语句**（保证块判定的依据），不是赋值语句本身。
                    # 记成 `s` 的话，`try: x = f() except: continue` 这种再也不会被判为保证块。
                    assigns.setdefault(n, []).append((id(stmts), owner, s.lineno))
                _R(anc, id(stmts)).visit(s)
                if depth < 40:
                    for attr in ("body", "orelse", "finalbody"):
                        v = getattr(s, attr, None)
                        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                            walk(v, anc + (id(stmts),), s, depth + 1)
                    for h in getattr(s, "handlers", None) or []:
                        walk(h.body, anc + (id(stmts),), s, depth + 1)
                    # 嵌套函数是独立作用域 —— 在里面读到外层名 = 闭包自由变量引用（本案炸点）
                    for sub in ast.walk(s):
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not s:
                            walk(sub.body, (), None, depth + 1)

        walk(fn.body, (), None, 0)

        for name in sorted(n for n in assigns if n in self.mod and n not in params):
            blks = assigns[name]
            for anc, rline in reads.get(name, []):
                ok = False
                for blk, owner, _ln in blks:
                    if blk in anc:            # A. 赋值块包住读取点
                        ok = True
                        break
                    if owner is not None and _guaranteed(owner, name) and (owner.end_lineno or 0) < rline:
                        ok = True            # B. 保证块整体在读取点之前结束
                        break
                if not ok:
                    self.hits.append((fn.lineno, fn.name, name,
                                      sorted({ln for _, _, ln in blks}), rline))
                    break


def main() -> None:
    files = [
        p for p in ROOT.rglob("*.py")
        if not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
    ]
    out: list[str] = []
    n = 0
    for p in sorted(files):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e:
            out.append(f"[跳过] {p.relative_to(ROOT)}: {e}")
            continue
        sc = ShadowScan(p, module_bound(tree))
        sc.run(tree)
        for fl, fname, var, alines, rline in sc.hits:
            n += 1
            out.append(
                f"{p.relative_to(ROOT)}:{fl}  {fname}()  变量 `{var}`（模块级已绑定）"
                f"  局部赋值行={alines}  越界读取行={rline}"
            )
    (ROOT / "_shadow_probe.txt").write_text(
        f"扫描 {len(files)} 个文件，命中 {n} 处\n\n" + "\n".join(out), encoding="utf-8"
    )
    print(f"扫描 {len(files)} 个文件，命中 {n} 处 → _shadow_probe.txt")


if __name__ == "__main__":
    main()

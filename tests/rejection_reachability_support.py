"""AST discovery for the rejection reachability gate; no runtime allowlist of codes.

Register modules and executable producer/mutation cases in the registry. Code
discovery is independent of those cases, so a new guard cannot silently inherit
coverage from another guard with the same spelling.
"""
import ast
from dataclasses import dataclass
import re
import sys

CODE = re.compile(r'^[A-Z][A-Z0-9_]{3,}$')


def rejection_sites(source):
    tree = ast.parse(source)
    constants = {n.targets[0].id: n.value for n in tree.body if isinstance(n, ast.Assign)
                 and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
    sites = {}

    def strings(node, bindings):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            if node.id in bindings:
                return bindings[node.id]
            if node.id in constants and constants[node.id] is not node:
                return strings(constants[node.id], bindings)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return set().union(*(strings(n, bindings) for n in node.elts))
        if isinstance(node, ast.IfExp):
            return strings(node.body, bindings) | strings(node.orelse, bindings)
        if isinstance(node, ast.BoolOp):
            return set().union(*(strings(n, bindings) for n in node.values))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return {a+b for a in strings(node.left, bindings) for b in strings(node.right, bindings)}
        if isinstance(node, ast.JoinedStr):
            values={''}
            for part in node.values:
                fragment=strings(part.value if isinstance(part,ast.FormattedValue) else part,bindings)
                values={left+right for left in values for right in fragment}
            return values
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'upper':
            return {s.upper() for s in strings(node.func.value, bindings)}
        return set()

    def visit(node, scope='', bindings=None):
        bindings = {} if bindings is None else bindings
        if isinstance(node, ast.ClassDef):
            scope = scope + node.name + '.'
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = scope + node.name
            bindings = dict(bindings)
            # Boolean-only validators still have to be explicitly registered.
            boolean_annotation=isinstance(node.returns,ast.Name) and node.returns.id=='bool'
            def own_statements(parent):
                for child in ast.iter_child_nodes(parent):
                    if not isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                        yield child
                        yield from own_statements(child)
            boolean_return=any(isinstance(child,ast.Return) and isinstance(child.value,ast.Constant)
                               and type(child.value.value) is bool for child in own_statements(node))
            predicate_name=(re.search(r'(^_?valid_|^_?validate_|_valid$|^accepts|^matches$|available$)',node.name)
                            or node.name in {'covers_suspect','keep_requires_audio'})
            if boolean_annotation or boolean_return or predicate_name:
                sites.setdefault(scope, set())
            for child in node.body:
                visit(child, scope + '.<locals>.', bindings)
            return
        owner = scope.removesuffix('.<locals>.')
        expressions = []
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
            if name == 'require' and len(node.args) > 1:
                expressions.append(node.args[1])
            if name == '_uncertain' and len(node.args) > 1:
                expressions.append(node.args[1])
            successful = any(kw.arg == 'status' and strings(kw.value,bindings)
                             and strings(kw.value,bindings) <= {'OBSERVED','CONTEXT_REVIEW','ARGON_OBSERVED','ARGON_PENDING'}
                             for kw in node.keywords)
            for kw in node.keywords if not successful else ():
                if kw.arg in {'reason', 'reason_code', 'category'}:
                    expressions.append(kw.value)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            expressions.append(node.exc.args[0])
        if isinstance(node, ast.Dict):
            # A nested route-state explanation is not a validation rejection.
            state_label = any(isinstance(k,ast.Constant) and (k.value == 'state' or
                k.value == 'status' and isinstance(v,ast.Constant) and v.value == 'NOT_REQUIRED')
                for k,v in zip(node.keys,node.values))
            if not state_label:
                expressions += [v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and k.value in {'reason', 'reason_code', 'category'}]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in {'reason', 'reason_code'}:
                    expressions.append(node.value)
                if isinstance(target, ast.Attribute) and target.attr == 'blocked':
                    expressions.append(node.value)
                if isinstance(target, ast.Name):
                    bindings[target.id] = bindings.get(target.id,set()) | strings(node.value, bindings)
        if isinstance(node, ast.Return):
            if isinstance(node.value, ast.Tuple) and len(node.value.elts) > 1 and isinstance(node.value.elts[0], ast.Constant) and node.value.elts[0].value is False:
                expressions.append(node.value.elts[1])
            if 'error' in owner or 'diagnostic' in owner:
                expressions.append(node.value)
        codes = {code for expr in expressions if expr is not None for code in strings(expr, bindings) if CODE.fullmatch(code)}
        if codes and owner:
            sites.setdefault(owner, set()).update(codes)
        if isinstance(node, ast.For):
            # Finite literal producer domains also cover formatted rejection codes.
            if isinstance(node.target, ast.Name):
                bindings[node.target.id] = strings(node.iter, bindings)
            if isinstance(node.target, ast.Tuple) and isinstance(node.iter, (ast.Tuple, ast.List)):
                for i, target in enumerate(node.target.elts):
                    if isinstance(target, ast.Name):
                        bindings[target.id] = set().union(*(strings(row.elts[i], bindings) for row in node.iter.elts if isinstance(row, (ast.Tuple, ast.List))))
        for child in ast.iter_child_nodes(node):
            visit(child, scope, bindings)

    visit(tree)
    return sites


@dataclass(frozen=True)
class ReachabilityCase:
    function: str
    code: str
    producer: object
    mutate: object
    consume: object
    accepted: object
    description: str


def emitted_code(call, function=None, *, boolean=False):
    caught=[]
    module,target=function.split('.',1) if function else (None,None)
    entered=[]
    def trace(frame,event,arg):
        is_target=(target and frame.f_code.co_qualname==target
                   and frame.f_globals.get('__name__')=='src.autoslice.'+module)
        if event=='call' and is_target:
            entered.append(True)
        if event=='exception' and is_target:
            exc=arg[1]
            value=str(getattr(exc,'reason',None) or getattr(exc,'reason_code',None) or exc)
            if CODE.fullmatch(value):
                caught.append(value)
        return trace
    previous=sys.gettrace()
    sys.settrace(trace)
    try:
        result = call()
    except Exception as exc:
        if target and not entered:
            return 'TARGET_NOT_CALLED'
        return str(getattr(exc, 'reason', None) or getattr(exc, 'reason_code', None) or exc)
    finally:
        sys.settrace(previous)
    if target and not entered:
        return 'TARGET_NOT_CALLED'
    if boolean:
        return '<FALSE>' if result is False else None
    if caught:
        return caught[-1]
    if result is False:
        return caught[-1] if caught else '<FALSE>'
    if isinstance(result, dict):
        return result.get('reason_code') or result.get('reason')
    if isinstance(result, tuple):
        if len(result) == 3 and result[0] is False:
            return result[1]
        for item in reversed(result):
            if isinstance(item, dict) and ('reason_code' in item or 'reason' in item):
                return item.get('reason_code') or item.get('reason')
    return caught[-1] if caught else None

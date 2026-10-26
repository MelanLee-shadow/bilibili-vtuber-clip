"""Scan shell and runpy import boundaries without installer/deploy effects."""
import ast
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = r'(?:python(?:3(?:\.\d+)?)?|\S+/python(?:3(?:\.\d+)?)?|repo_python|"\$[A-Z_]*PYTHON[A-Z_]*")'
HEREDOC = re.compile(rf'(?m)^[^\n]*(?<!\w){PYTHON}\s+[^\n]*?<<-?\s*[\'"]?(\w+)[\'"]?[^\n]*\n')


def shell_python_blocks(text):
    """Find inner Python heredocs too, inside remote shell heredocs/functions."""
    joined = text.replace('\\\n', ' ')
    for match in HEREDOC.finditer(joined):
        end = re.search(rf'(?m)^\s*{re.escape(match[1])}\s*$', joined[match.end():])
        assert end is not None, match[0]
        yield joined[:match.start()].count('\n') + 1, match[0], joined[match.end():match.end() + end.start()]
    for number, line in enumerate(joined.splitlines(), 1):
        if re.search(rf'(?<!\w){PYTHON}\s+.*?-c\s+', line):
            # shlex handles quotes containing multiple imports/semicolons.
            argument = re.search(r'''\s-c\s+(?P<quote>['"])(?P<code>(?:\\.|(?!(?P=quote)).)*)(?P=quote)''', line)
            if argument:
                yield number, line, argument['code'].replace('\\"', '"')


def repo_imports(code):
    if not re.search(r'\b(?:from|import)\s+src\b|runpy\.run_path|(?:__import__|import_module)\(', code):
        return ast.parse(''), []
    tree = ast.parse(code)
    imports = sorted((node for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module == 'src' or (node.module or '').startswith('src.'))
        or isinstance(node, ast.Import) and any(alias.name == 'src' or alias.name.startswith('src.') for alias in node.names)
        or isinstance(node, ast.Call) and ast.unparse(node.func) in {'__import__', 'importlib.import_module'}
        and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        and (node.args[0].value == 'src' or node.args[0].value.startswith('src.'))),
        key=lambda node: node.lineno)
    return tree, imports


def minimal_import_script(code, *, repo, source_file=None):
    tree, imports = repo_imports(code)
    # Only run the actual sys.path bootstrap and imports; never runtime calls.
    inserts = [node for node in ast.walk(tree) if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == 'sys.path.insert']
    needed = set()
    selected = []
    for node in inserts:
        needed.update(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
    for node in reversed(tree.body):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in needed for target in node.targets):
            selected.append(node)
            needed.update(n.id for n in ast.walk(node.value) if isinstance(n, ast.Name))
    selected.extend(inserts)
    selected.extend(imports if source_file is None else imports[:1])
    selected.sort(key=lambda node: (node.lineno, node.col_offset))
    prelude = 'import sys, importlib\nfrom pathlib import Path\nsys.argv = ["probe", ' + repr(str(repo)) + '] * 8\n'
    if source_file is not None:
        prelude += '__file__ = ' + repr(str(source_file)) + '\n'
    return prelude + '\n'.join(ast.unparse(node) for node in selected)


def shell_python_file_entries(text, root):
    """File-backed children and generated cron/array launchers use the same guard."""
    for number, line in enumerate(text.replace('\\\n', ' ').splitlines(), 1):
        if (not re.search(r'python|PYTHON', line)
                or re.match(r'\s*(?:#|echo\b|for\s+pid\b)', line)):
            continue
        for name in dict.fromkeys(re.findall(r'/([A-Za-z0-9_.-]+\.py)\b', line)):
            path = root / 'scripts' / name
            if path.is_file():
                yield number, path


def assert_file_bootstrap(path, tmp_path):
    code = path.read_text()
    tree, imports = repo_imports(code)
    if not imports:
        return False
    inserts = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and ast.unparse(node.func) == 'sys.path.insert' and node.lineno < imports[0].lineno]
    assert inserts, f'{path}: unbootstrapped repo import in Python file entry'
    script = tmp_path / 'file-entry-minimal-import.py'
    script.write_text(minimal_import_script(code, repo=path.parent.parent, source_file=path))
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    completed = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
        env=environment, text=True, capture_output=True, timeout=60)
    assert completed.returncode == 0, f'{path}: {completed.stderr}'
    return True


def assert_bootstrap(path, number, command, code, text, tmp_path):
    tree, imports = repo_imports(code)
    indirect = 'runpy.run_path' in code and ('src/' in command or 'src/' in code)
    if not imports and not indirect:
        return False
    first = imports[0].lineno if imports else 10**9
    inserts = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and ast.unparse(node.func) == 'sys.path.insert' and node.lineno < first]
    shared = bool(re.search(r'\brepo_python\s+-', command))
    # These installer checks deliberately cd to their exact staged version.
    # Preserve that existing boundary and exercise it in a disposable shell.
    previous = text.replace('\\\n', ' ').splitlines()[:number - 1]
    last_boundary = max((i for i, line in enumerate(previous)
        if re.search(r"<<[\'\"]?REMOTE|^\w+$", line)), default=-1)
    cd = next((line.strip() for line in reversed(previous[last_boundary + 1:])
        if re.fullmatch(r'cd "\$1"', line.strip())), None)
    if 'cd "$version" &&' in command:
        cd = 'cd "$version"'
    assert inserts or shared or cd, f'{path}:{number}: unbootstrapped repo import'
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    repo_arg = ROOT
    if any(' / \'repo\'' in ast.unparse(node) or ' / "repo"' in ast.unparse(node) for node in inserts):
        runtime = tmp_path / 'runtime'
        runtime.mkdir(exist_ok=True)
        (runtime / 'repo').symlink_to(ROOT, target_is_directory=True)
        repo_arg = runtime
    script = tmp_path / 'minimal-import.py'
    script.write_text(minimal_import_script(code, repo=repo_arg))
    launch = f'{shlex.quote(sys.executable)} -c {shlex.quote(script.read_text())}'
    if shared:
        definition = re.search(r'repo_python\(\) \{\n.*?\n\}', text, re.S)
        assert definition and 'sys.path.insert(0, sys.argv[1])' in definition[0]
        assert '"$REPO_ROOT" "$@"' in definition[0]
        root_binding = re.search(r'(?m)^REPO_ROOT=.+$', text)
        assert root_binding, f'{path}:{number}: missing script-relative root binding'
        # Execute the production function, replacing only its inline body with
        # minimal imports. The foreign cwd and absent inherited path stay real.
        launch = f'{root_binding[0]}\n{definition[0]}\nrepo_python - <<\'MINIMAL_IMPORT\'\n{script.read_text()}\nMINIMAL_IMPORT\n'
    elif cd:
        launch = f'set -- {shlex.quote(str(ROOT))}; version=$1; {cd} && ' + launch
    if shared:
        version = tmp_path / 'version'
        (version / 'scripts').mkdir(parents=True, exist_ok=True)
        if not (version / 'src').exists():
            (version / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
        probe = version / 'scripts' / 'probe.sh'
    else:
        probe = tmp_path / 'probe.sh'
    probe.write_text(launch)
    result = subprocess.run(['bash', str(probe)], cwd=tmp_path, env=environment,
        text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, f'{path}:{number}: {result.stderr}'
    return True


def scan_directory(directory, tmp_path):
    found = []
    for path in sorted(directory.rglob('*.sh')):
        text = path.read_text()
        for number, command, code in shell_python_blocks(text):
            if assert_bootstrap(path, number, command, code, text, tmp_path):
                found.append((path.name, number))
        for number, entry in shell_python_file_entries(text, directory.parent):
            if assert_file_bootstrap(entry, tmp_path):
                found.append((path.name, number))
    return found


def test_all_shell_repo_imports_have_working_bootstrap(tmp_path):
    found = scan_directory(ROOT / 'scripts', tmp_path)
    assert found, 'scanner must discover real production imports'


def test_new_shell_member_is_discovered_and_missing_bootstrap_is_rejected(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    member = scripts / 'new-member.sh'
    for launch in ('python3', '/usr/bin/python3'):
        for statement in ('from src.autoslice.cpa_runtime import record_cpa_event',
                          'from src import autoslice', 'import src.autoslice.cpa_runtime',
                          '__import__("src.autoslice.cpa_runtime")'):
            text = f"{launch} - <<'NEW_CASE'\n{statement}\nNEW_CASE\n"
            member.write_text(text)
            with pytest.raises(AssertionError, match='unbootstrapped'):
                scan_directory(scripts, tmp_path)
    definition = re.search(r'repo_python\(\) \{\n.*?\n\}', (ROOT / 'scripts/llm_via_cpa.sh').read_text(), re.S)
    # A new script declares the same ordinary bootstrap, without scanner registration.
    text = 'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"\n' + definition[0] + '\nrepo_python - <<\'NEW_CASE\'\nfrom src.autoslice.cpa_runtime import record_cpa_event\nNEW_CASE\n'
    member.write_text(text)
    assert scan_directory(scripts, tmp_path) == [('new-member.sh', 5)]


def test_new_file_backed_child_enters_the_same_scan_and_import_guard(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (tmp_path / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
    entry = scripts / 'new_entry.py'
    member = scripts / 'new-member.sh'
    member.write_text('python3 "$ROOT/scripts/new_entry.py"\n')
    entry.write_text('from src.autoslice.cpa_runtime import record_cpa_event\n')
    with pytest.raises(AssertionError, match='unbootstrapped'):
        scan_directory(scripts, tmp_path)
    entry.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nfrom src.autoslice.cpa_runtime import record_cpa_event\n')
    assert scan_directory(scripts, tmp_path) == [('new-member.sh', 1)]


# These calls enter the stdlib lease bootstrap before repository imports are
# safe. The wrapper acquires/pins the lease; its target CLIs bootstrap their own
# imports. Exempt only these caller/target pairs, never every call in a file.
LEASE_BOOTSTRAP_CALLERS = {
    'scripts/produce_slice_package.py', 'scripts/boundary_retry_entry.py',
    'scripts/session_autoslice.py', 'scripts/run_publication_queue.py',
    'src/autoslice/__init__.py',
}


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_name(node.value) + '.' + node.attr
    return ''


def _static_value(node, bindings):
    """Evaluate only path construction; never execute repository code."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bindings[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_static_value(node.operand, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _static_value(node.left, bindings) / _static_value(node.right, bindings)
    if isinstance(node, ast.Attribute) and node.attr in {'parent', 'parents'}:
        return getattr(_static_value(node.value, bindings), node.attr)
    if isinstance(node, ast.Subscript):
        return _static_value(node.value, bindings)[_static_value(node.slice, bindings)]
    if isinstance(node, ast.Call):
        if _call_name(node.func) in {'Path', '_Path', 'str'} and len(node.args) == 1:
            value = _static_value(node.args[0], bindings)
            return str(value) if _call_name(node.func) == 'str' else Path(value)
        if (isinstance(node.func, ast.Attribute) and node.func.attr in {'resolve', 'absolute'}
                and not node.args and not node.keywords):
            return getattr(_static_value(node.func.value, bindings), node.func.attr)()
    raise ValueError(f'unresolved path expression: {ast.unparse(node)}')


def _preceding_statements(node, parents):
    """Statements that dominate the call, excluding conditional sibling bodies."""
    result = []
    while node in parents:
        parent = parents[node]
        for _, value in ast.iter_fields(parent):
            if isinstance(value, list) and node in value:
                result[:0] = value[:value.index(node)]
        node = parent
    return result


def _path_context(statements, path, tree, call, root, *, symbolic=False):
    bindings = {'__file__': str(path)}
    # Model the repository supplied to a loader function symbolically. Only
    # expressions relative to that argument qualify; cwd/environment do not.
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    node = call
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                bindings[arg.arg] = root / '.runpy-argument' / arg.arg if symbolic else root
    inserts = []
    for statement in statements:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.pop(target.id, None)
                    try:
                        bindings[target.id] = _static_value(statement.value, bindings)
                    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                        pass
        if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                and _call_name(statement.value.func) == 'sys.path.insert'):
            insertion = statement.value
            if len(insertion.args) == 2:
                try:
                    index, value = (_static_value(arg, bindings) for arg in insertion.args)
                    if index == 0:
                        inserts.append(Path(value).resolve())
                except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                    pass
    return bindings, inserts


def _target_imports_src(target, root):
    code = target.read_text()
    _, imports = repo_imports(code)
    if imports:
        return True
    tree = ast.parse(code)
    # One local import hop also counts: a runpy script can import a helper
    # which then imports src. Resolve packages and relative imports as files.
    package = list(target.relative_to(root).with_suffix('').parts[:-1])
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name.split('.') for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[:len(package) - node.level + 1] if node.level else []
            module = prefix + (node.module.split('.') if node.module else [])
            modules.append(module)
            modules.extend(module + alias.name.split('.') for alias in node.names)
    for parts in modules:
        for base in (root, target.parent):
            module = base.joinpath(*parts)
            for candidate in (module.with_suffix('.py'), module / '__init__.py'):
                if candidate.is_file() and repo_imports(candidate.read_text())[1]:
                    return True
    return False


def scan_runpy_directory(root):
    """Discover every run_path call, including import aliases and new members."""
    found = []
    for directory in ('src', 'scripts'):
        for path in sorted((root / directory).rglob('*.py')):
            tree = ast.parse(path.read_text(), filename=str(path))
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            runpy_names, run_path_names = set(), set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    runpy_names.update(alias.asname or alias.name for alias in node.names if alias.name == 'runpy')
                elif isinstance(node, ast.ImportFrom) and node.module == 'runpy':
                    run_path_names.update(alias.asname or alias.name for alias in node.names if alias.name == 'run_path')
            names = {name + '.run_path' for name in runpy_names} | run_path_names
            for call in sorted((node for node in ast.walk(tree) if isinstance(node, ast.Call)
                                and _call_name(node.func) in names), key=lambda node: node.lineno):
                label = f'{path.relative_to(root)}:{call.lineno}'
                argument = call.args[0] if call.args else next(
                    (kw.value for kw in call.keywords if kw.arg == 'path_name'), None)
                assert argument is not None, f'{label}: unresolved runpy target'
                relative = path.relative_to(root).as_posix()
                if relative == 'src/autoslice/runtime_code_lease.py' and ast.unparse(argument) == 'str(script)':
                    found.append((relative, call.lineno, '<leased CLI>', 'lease bootstrap'))
                    continue
                preceding = _preceding_statements(call, parents)
                bindings, _ = _path_context(preceding, path, tree, call, root)
                symbolic, inserts = _path_context(preceding, path, tree, call, root, symbolic=True)
                try:
                    target = Path(_static_value(argument, bindings)).resolve()
                except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
                    raise AssertionError(f'{label}: unresolved runpy target') from exc
                if relative in LEASE_BOOTSTRAP_CALLERS and target == root.parent / 'runtime-code-lease.py':
                    found.append((relative, call.lineno, str(target), 'stdlib lease bootstrap'))
                    continue
                assert target.is_relative_to(root) and target.is_file(), f'{label}: unresolved runpy target {target}'
                needs_bootstrap = _target_imports_src(target, root)
                symbolic_target = Path(_static_value(argument, symbolic)).resolve()
                target_repo = symbolic_target.parents[len(target.relative_to(root).parts) - 1]
                assert not needs_bootstrap or target_repo in inserts, f'{label}: unbootstrapped runpy repo import ({target})'
                found.append((relative, call.lineno, str(target), 'repo bootstrap' if needs_bootstrap else 'no src import'))
    return found


def test_all_runpy_repo_imports_have_fixed_version_bootstrap():
    found = scan_runpy_directory(ROOT)
    assert any(row[0] == 'src/autoslice/overflow_install.py' for row in found)
    assert sum(row[3] == 'lease bootstrap' for row in found) == 2
    assert {row[0] for row in found if row[3] == 'stdlib lease bootstrap'} == LEASE_BOOTSTRAP_CALLERS


@pytest.mark.parametrize('load', [
    'import runpy',
    'import runpy as runner',
    'from runpy import run_path as load',
])
@pytest.mark.parametrize('indirect', [False, True])
def test_new_runpy_member_requires_same_repo_before_loading(tmp_path, load, indirect):
    (tmp_path / 'src').mkdir()
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (tmp_path / 'src/dependency.py').write_text('VALUE = 7\n')
    target = scripts / 'new_target.py'
    dependency_import = 'from src import dependency\nprint(dependency.__file__)\n'
    target.write_text(dependency_import if not indirect else 'import helper\n')
    if indirect:
        (scripts / 'helper.py').write_text(dependency_import)
    member = scripts / 'new_loader.py'
    function = 'runner.run_path' if 'as runner' in load else ('load' if 'from runpy' in load else 'runpy.run_path')
    head = 'import sys\nfrom pathlib import Path\n' + load + '\nROOT = Path(__file__).resolve().parents[1]\n'
    call = f'{function}(path_name=str(ROOT / "scripts/new_target.py"))\n'
    for guard in ('', 'sys.path.insert(0, str(Path.cwd()))\n',
                  'sys.path.insert(0, str(ROOT.parent))\n',
                  'if False:\n    sys.path.insert(0, str(ROOT))\n'):
        member.write_text(head + guard + call)
        with pytest.raises(AssertionError, match='unbootstrapped'):
            scan_runpy_directory(tmp_path)
    member.write_text(head + call + 'sys.path.insert(0, str(ROOT))\n')
    with pytest.raises(AssertionError, match='unbootstrapped'):
        scan_runpy_directory(tmp_path)
    member.write_text(head + 'sys.path.insert(0, str(ROOT))\n' + call)
    assert scan_runpy_directory(tmp_path)[0][3] == 'repo bootstrap'
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    result = subprocess.run([sys.executable, '-B', str(member)], cwd=foreign,
                            env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == tmp_path / 'src/dependency.py'
    # An already-correct stdlib-only target stays outside the repair class.
    target.write_text('import json\n')
    member.write_text(head + call)
    assert scan_runpy_directory(tmp_path)[0][3] == 'no src import'


def test_runpy_bootstrap_must_use_the_target_repository_argument(tmp_path):
    (tmp_path / 'src').mkdir()
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'target.py').write_text('from src import dependency\n')
    member = scripts / 'loader.py'
    code = ('import sys, runpy\n'
            'def load(repo, other_repo):\n'
            '    sys.path.insert(0, str(OTHER))\n'
            '    runpy.run_path(str(repo / "scripts/target.py"))\n')
    member.write_text(code.replace('OTHER', 'other_repo'))
    with pytest.raises(AssertionError, match='unbootstrapped'):
        scan_runpy_directory(tmp_path)
    member.write_text(code.replace('OTHER', 'repo'))
    assert scan_runpy_directory(tmp_path)[0][3] == 'repo bootstrap'

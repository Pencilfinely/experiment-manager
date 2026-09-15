"""Conservative, static entry-point discovery; never import or run research code.

The report is a configuration draft, not a claim that arbitrary programs can be
run or resumed automatically. All paths in the draft are relative to ``source``.
"""
from __future__ import annotations

import ast
import math
import os
from pathlib import Path
import re
import sys
import tokenize


_IGNORED = frozenset({'.git', '.hg', '.svn', '.idea', '.vscode', '__pycache__',
    '.venv', 'venv', 'node_modules', 'output', 'outputs', 'runs', 'logs',
    'checkpoints', 'data', 'datasets', '.pytest_cache', 'dist', 'build'})
_ENTRY_NAMES = {'main.py': 100, 'train.py': 90, 'run.py': 80, 'start.py': 75,
                '__main__.py': 70}
_MAX_DEPTH = 4
_MAX_FILES = 400
_MAX_FILE_BYTES = 1024 * 1024
_UNKNOWN = object()


def _literal(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _UNKNOWN
    # Only emit values that can be represented by a JSON configuration.
    def json_value(item):
        if item is None or isinstance(item, (str, bool, int)):
            return True
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, (list, tuple)):
            return all(json_value(v) for v in item)
        if isinstance(item, dict):
            return all(isinstance(k, str) and json_value(v) for k, v in item.items())
        return False
    return value if json_value(value) else _UNKNOWN


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return (prefix + '.' if prefix else '') + node.attr
    return ''


def _role(dest):
    key = dest.lower().replace('-', '_')
    if re.search(r'(^|_)(resume|restore)(_|$)', key):
        return 'resume_candidate'
    if key in {'do_eval', 'eval', 'evaluate', 'test_only', 'eval_only'}:
        return 'evaluation'
    if key in {'gpu', 'gpu_id', 'device', 'cuda_device', 'local_rank', 'no_cuda'}:
        return 'device'
    if key in {'config', 'config_file', 'config_path', 'cfg'}:
        return 'config_file'
    if re.search(r'(^|_)(output|save|log|checkpoint)(_|$)', key) and re.search(r'(dir|root|path)$', key):
        return 'output_path'
    if re.search(r'(^|_)(data|dataset)(_|$)', key) and re.search(r'(dir|root|path|file)$', key):
        return 'data_path'
    if key in {'data_name', 'dataset', 'dataset_name'}:
        return 'dataset'
    if 'checkpoint' in key or key in {'weights', 'load_model'}:
        return 'checkpoint_candidate'
    return 'parameter'


def _arguments(tree, warnings):
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'add_argument':
            continue
        flags = [_literal(v) for v in node.args]
        if not flags or any(not isinstance(v, str) for v in flags):
            warnings.append(f'Line {node.lineno}: dynamic add_argument flags require manual configuration.')
            continue
        values, dynamic = {}, []
        for keyword in node.keywords:
            if keyword.arg is None:
                dynamic.append('**kwargs')
                continue
            if keyword.arg == 'type':
                value = _dotted(keyword.value)
                if value not in {'str', 'int', 'float', 'bool'}:
                    dynamic.append('type')
                    value = None
            else:
                value = _literal(keyword.value)
                if value is _UNKNOWN:
                    dynamic.append(keyword.arg)
                    value = None
            values[keyword.arg] = value
        flag = next((f for f in flags if f.startswith('--')), flags[0])
        dest = values.get('dest') or flag.lstrip('-').replace('-', '_')
        action = values.get('action', 'store')
        if not isinstance(dest, str) or not isinstance(action, (str, type(None))):
            warnings.append(f'Line {node.lineno}: invalid literal argument dest/action requires manual configuration.')
            continue
        default = values.get('default')
        if 'default' not in values and action in {'store_true', 'store_false'}:
            default = action == 'store_false'
        required = values.get('required', not flags[0].startswith('-') and values.get('nargs') not in ('?', '*'))
        type_name = values.get('type')
        if type_name is None and 'type' not in values:
            type_name = ('bool' if action in {'store_true', 'store_false'} else
                         'int' if action == 'count' else 'str')
        result.append({'flags': flags, 'dest': dest, 'default': default,
            'default_provided': 'default' in values or action in {'store_true', 'store_false'},
            'type': type_name, 'action': action, 'required': bool(required),
            'choices': values.get('choices'), 'help': values.get('help'),
            'nargs': values.get('nargs'), 'inferred_role': _role(dest),
            'confidence': 'review' if dynamic else 'literal',
            'dynamic_fields': dynamic, 'source_line': node.lineno})
        if dynamic:
            warnings.append(f'Argument {flag}: dynamic fields {", ".join(dynamic)} were not executed; review their values.')
        if action not in {'store', 'store_true', 'store_false'} or values.get('nargs') is not None:
            warnings.append(f'Argument {flag}: action/nargs needs an explicit harness binding.')
    return sorted(result, key=lambda item: item['source_line'])


def _files(root, warnings):
    files = []
    for base, directories, names in os.walk(root, followlinks=False):
        base = Path(base)
        depth = len(base.relative_to(root).parts)
        def allowed_directory(name):
            candidate = base / name
            if name.lower() in _IGNORED or candidate.is_symlink():
                return False
            # Windows junctions may not report as symlinks on older Python.
            try:
                candidate.resolve().relative_to(root)
            except (OSError, ValueError, RuntimeError):
                return False
            return True
        directories[:] = sorted(d for d in directories if allowed_directory(d)) if depth < _MAX_DEPTH else []
        for name in sorted(names):
            path = base / name
            if path.is_symlink() or path.suffix.lower() not in {'.py', '.bat', '.cmd', '.sh'}:
                continue
            if len(files) >= _MAX_FILES:
                warnings.append(f'Scan stopped after {_MAX_FILES} source files; select and review an entry explicitly.')
                return files
            if path.stat().st_size > _MAX_FILE_BYTES:
                warnings.append(f'Skipped source larger than {_MAX_FILE_BYTES} bytes: {path.relative_to(root).as_posix()}')
                continue
            files.append(path)
    return files


def _entry(path, root, explicit=False):
    relative = path.relative_to(root)
    suffix = path.suffix.lower()
    kind = {'.py': 'python', '.sh': 'bash', '.bat': 'windows-batch', '.cmd': 'windows-batch'}[suffix]
    prefix = {'python': ['{python}'], 'bash': ['bash'], 'windows-batch': ['cmd.exe', '/d', '/c']}[kind]
    score = _ENTRY_NAMES.get(path.name.lower(), 50 if suffix != '.py' else 10)
    return {'path': relative.as_posix(), 'kind': kind,
        'command': prefix + [path.name], 'cwd': relative.parent.as_posix(),
        'confidence': 'explicit' if explicit else 'filename',
        '_score': score - len(relative.parts)}


def _gpu_assignment(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Subscript) or _dotted(target.value) != 'os.environ':
                continue
            if _literal(target.slice) != 'CUDA_VISIBLE_DEVICES':
                continue
            return {'overwritten': True,
                'argument': node.value.attr if isinstance(node.value, ast.Attribute) else None,
                'source_line': node.lineno}
    return {'overwritten': False, 'argument': None}


def _path_hints(tree, arguments):
    candidates = {a['dest'] for a in arguments if a['inferred_role'] in {'data_path', 'output_path'}
                  and re.search(r'(dir|directory|root)$', a['dest'], re.I)}
    hints = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
            continue
        left = node.left
        if isinstance(left, ast.Attribute) and left.attr in candidates:
            right = _literal(node.right)
            if isinstance(right, str) and right.startswith(('/', '\\')):
                continue
            hints[left.attr] = {'dest': left.attr, 'trailing_separator': True,
                'evidence': f'Line {node.lineno}: directory is joined by string addition; preserve a trailing separator.'}
    return list(hints.values())


def _metric_hints(tree, relative):
    hints = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [_literal(key) for key in node.keys if key is not None]
        keys = [key for key in keys if isinstance(key, str)]
        step = next((key for key in keys if key.lower() in {'epoch', 'step', 'global_step', 'iteration'}), None)
        values = [key for key in keys if key != step and
                  re.search(r'loss|accuracy|acc$|recall|precision|ndcg|hit@|mrr|rmse|mae|auc|f1|perplexity', key, re.I)]
        if step and values:
            hints.append({'file': relative, 'source_line': node.lineno,
                'step_key': step, 'value_keys': values, 'format': 'python_dict_candidate',
                'confidence': 'review', 'note': 'Literal dictionary keys only; verify this dictionary is actually printed or logged.'})
    return hints


def discover_project(source, entry=None):
    """Return a JSON-serializable configuration draft without executing source.

    ``entry`` may be a project-relative path or an absolute path inside the project.
    Symlink entries, paths outside the source, and unsupported entry suffixes are
    rejected. Static extraction never guesses that loading weights means resume.
    """
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f'Source directory does not exist: {root}')
    warnings = []
    selected_path = None
    if entry is not None:
        candidate = Path(entry)
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.is_symlink():
            raise ValueError('Entry must not be a symlink')
        selected_path = candidate.resolve()
        try:
            selected_path.relative_to(root)
        except ValueError as exc:
            raise ValueError('Entry must be inside the source directory') from exc
        if not selected_path.is_file() or selected_path.suffix.lower() not in {'.py', '.bat', '.cmd', '.sh'}:
            raise ValueError('Entry must be an existing .py, .sh, .bat or .cmd file')
        if selected_path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError('Entry is too large for static discovery')
    paths = _files(root, warnings)
    if selected_path is not None and selected_path not in paths:
        paths.append(selected_path)
    entries = [_entry(p, root, p == selected_path) for p in paths
               if p.name.lower() in _ENTRY_NAMES or p.suffix.lower() != '.py' or p == selected_path]
    entries.sort(key=lambda item: (-item['_score'], item['path']))
    if selected_path is None and entries:
        selected_path = root / entries[0]['path']
        if len(entries) > 1:
            warnings.append('Multiple entry candidates found; the selected entry is a filename-based suggestion.')
    chosen = next((e for e in entries if root / e['path'] == selected_path), None)
    for value in entries:
        value.pop('_score')
    trees, imports, metric_hints = {}, set(), []
    for path in paths:
        if path.suffix.lower() != '.py':
            continue
        relative = path.relative_to(root).as_posix()
        try:
            with tokenize.open(path) as handle:
                tree = ast.parse(handle.read(), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            warnings.append(f'Cannot statically parse {relative}: {type(exc).__name__}')
            continue
        trees[path] = tree
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports.add(node.module.split('.')[0])
        metric_hints.extend(_metric_hints(tree, relative))
    tree = trees.get(selected_path)
    arguments = _arguments(tree, warnings) if tree is not None else []
    gpu = _gpu_assignment(tree) if tree is not None else {'overwritten': False, 'argument': None}
    paths_hints = _path_hints(tree, arguments) if tree is not None else []
    if gpu['overwritten']:
        warnings.append('Entry overwrites CUDA_VISIBLE_DEVICES' +
            (f' from argument {gpu["argument"]}' if gpu['argument'] else '') +
            '; map the assigned GPU UUID through that argument rather than assuming the inherited environment survives.')
    warnings.extend(h['evidence'] for h in paths_hints)
    if tree is not None:
        unguarded = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                     and _dotted(n.value.func) in {'main', 'train', 'run', 'start'}]
        if unguarded:
            warnings.append('Entry invokes training at module scope; discovery did not import it or run --help.')
    if chosen and chosen['kind'] == 'windows-batch':
        warnings.append('Windows batch entry requires Windows; Linux GPU containers need an equivalent Python/bash entry chosen explicitly.')
    if chosen and chosen['kind'] != 'python':
        warnings.append('Shell entry arguments were not inferred; configure its command and bindings manually.')
    if not chosen:
        warnings.append('No conventional entry found; pass entry= with the actual Python or shell entry.')
    local_modules = {p.stem for p in paths if p.suffix.lower() == '.py'}
    local_modules.update(p.relative_to(root).parts[0] for p in paths if len(p.relative_to(root).parts) > 1)
    standard = set(getattr(sys, 'stdlib_module_names', ())) | set(sys.builtin_module_names) | {'__future__'}
    dependencies = sorted(imports - standard - local_modules)
    if dependencies:
        warnings.append('Dependency hints are import names, not verified package names or versions; review the runtime environment.')
    resume_candidates = [a['dest'] for a in arguments if a['inferred_role'] in {'resume_candidate', 'checkpoint_candidate'}]
    if resume_candidates:
        warnings.append('Resume/checkpoint flags are review candidates only; native training resume remains disabled until explicitly configured.')
    if any(a['inferred_role'] == 'evaluation' for a in arguments):
        warnings.append('Evaluation flags (for example --do_eval) are not evidence of training resume support.')
    return {'schema_version': 1, 'source': str(root), 'entries': entries,
        'selected_entry': chosen['path'] if chosen else None,
        'command': chosen['command'] if chosen else [], 'cwd': chosen['cwd'] if chosen else '.',
        'arguments': arguments, 'imports': sorted(imports), 'dependency_hints': dependencies,
        'warnings': warnings, 'resume': {'supported': False, 'candidates': resume_candidates, 'requires_review': True},
        'gpu_visibility': gpu, 'path_hints': paths_hints, 'metric_hints': metric_hints,
        'source_policy': {'include': ['**/*.py', '**/*.sh', '**/*.bat', '**/*.cmd',
            'requirements*.txt', 'pyproject.toml', '**/*.json', '**/*.yaml', '**/*.yml', '**/*.toml'],
            'exclude_directories': sorted(_IGNORED),
            'note': 'Review source/configuration files before packaging; datasets, outputs and environments are separate.'}}

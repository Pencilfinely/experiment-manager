"""Compile the native Windows application or its self-extracting installer.

Requires the Windows .NET Framework C# compiler. A release build never substitutes
a command file or an uncompiled source file for a native application.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'deploy/desktop/ExperimentApp.cs'
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
EXTRA_SOURCES = (ROOT / 'deploy/desktop/DesktopUpdates.cs', ROOT / 'deploy/desktop/DesktopUpdateForm.cs')
ICONS = {'controller': ROOT / 'assets/center.ico', 'worker': ROOT / 'assets/worker.ico'}
REFERENCES = ('System.Windows.Forms.dll', 'System.Drawing.dll', 'System.Web.Extensions.dll',
              'System.IO.Compression.dll', 'System.IO.Compression.FileSystem.dll', 'Microsoft.CSharp.dll')


def find_compiler(supplied=None):
    if supplied:
        candidate = Path(supplied).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise RuntimeError('The supplied C# compiler does not exist: ' + str(candidate))
    windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
    for architecture in ('Framework64', 'Framework'):
        candidate = windows / 'Microsoft.NET' / architecture / 'v4.0.30319/csc.exe'
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError('Native Windows release build requires the .NET Framework C# compiler '
                       '(csc.exe). Build on Windows or supply --compiler; no placeholder EXE is produced.')


def compile_desktop(output, role, payload=None, compiler=None):
    if role not in ('controller', 'worker'):
        raise ValueError('Desktop role must be controller or worker')
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Refusing to replace a compiled application: ' + str(output))
    source = SOURCE.resolve(strict=True)
    extra_sources = [path.resolve(strict=True) for path in EXTRA_SOURCES]
    icon = ICONS[role].resolve(strict=True)
    if ',' in str(icon):
        raise ValueError('Embedded icon path cannot contain a comma')
    executable = find_compiler(compiler)
    payload = Path(payload).resolve(strict=True) if payload is not None else None
    if payload is not None and ',' in str(payload):
        raise ValueError('Embedded payload path cannot contain a comma')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / ('.desktop-build-' + uuid.uuid4().hex)
    temporary.mkdir()
    try:
        role_file = temporary / 'role.txt'
        role_file.write_text(role, encoding='utf-8')
        version_file = temporary / 'version.txt'
        version_file.write_text(VERSION, encoding='utf-8')
        target = temporary / output.name
        args = [str(executable), '/nologo', '/target:winexe', '/platform:x64', '/optimize+',
                '/out:' + str(target), '/resource:' + str(role_file) + ',Role',
                '/win32icon:' + str(icon), '/resource:' + str(icon) + ',AppIcon',
                '/resource:' + str(version_file) + ',AppVersion']
        args.extend('/reference:' + name for name in REFERENCES)
        if payload is not None:
            args.append('/resource:' + str(payload) + ',AppPayload')
        args.append(str(source))
        args.extend(str(path) for path in extra_sources)
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=180)
        if result.returncode:
            raise RuntimeError('Native application compilation failed:\n' +
                               (result.stdout + '\n' + result.stderr)[-8000:])
        if not target.is_file() or target.read_bytes()[:2] != b'MZ':
            raise RuntimeError('Compiler did not produce a Windows executable')
        # Exclusive creation makes concurrent/repeated releases fail visibly.
        with output.open('xb') as stream:
            stream.write(target.read_bytes())
    finally:
        if temporary.parent.resolve() != output.parent.resolve() or not temporary.name.startswith('.desktop-build-'):
            raise RuntimeError('Unexpected desktop build cleanup path')
        shutil.rmtree(temporary)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--role', required=True, choices=('controller', 'worker'))
    parser.add_argument('--payload', help='ZIP embedded into a self-extracting installer')
    parser.add_argument('--compiler')
    args = parser.parse_args()
    result = compile_desktop(args.output, args.role, args.payload, args.compiler)
    print('Built: ' + str(result))


if __name__ == '__main__':
    main()

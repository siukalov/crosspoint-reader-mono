import argparse
import csv
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections import Counter


EXCLUDED_PARTS = {
    '.pio', 'build', 'builtinfonts', 'deps', 'generated', 'node_modules',
    'third_party', 'thirdparty', 'vendor',
}
SOURCE_SUFFIXES = {'.c', '.cpp', '.h'}


def git(repo, *args):
    return subprocess.check_output(
        ['git', '-C', str(repo), *args], text=True, errors='surrogateescape'
    )


def write_table(title, header, rows):
    print(f'[{title}]')
    writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    print()


def file_churn(repo, since, bugs=False):
    args = ['log', f'--since={since}', '--format=', '--name-only', '-z']
    if bugs:
        args.extend(['-i', '-E', '--grep=fix|bug|broken'])
    return Counter(path for path in git(repo, *args).split('\0') if path)


def print_diagnosis(repo, since, limit, churn, bugs):
    write_table('churn', ['commits', 'file'],
                ((count, path) for path, count in churn.most_common(limit)))
    write_table('bug_hotspots', ['bug_commits', 'all_commits', 'file'],
                ((count, churn[path], path) for path, count in bugs.most_common(limit)))
    overlap = set(dict(churn.most_common(limit))) & set(dict(bugs.most_common(limit)))
    write_table('hotspot_overlap', ['commits', 'bug_commits', 'file'],
                ((churn[path], bugs[path], path)
                 for path in sorted(overlap, key=lambda p: (-churn[p], p))))
    authors = Counter(git(repo, 'log', '--no-merges', f'--since={since}',
                          '--format=%aN').splitlines())
    total = sum(authors.values())
    write_table('author_concentration', ['commits', 'percent', 'at_least_60_percent', 'author'],
                ((count, f'{100 * count / total:.1f}',
                  'yes' if count / total >= 0.6 else 'no', author)
                 for author, count in authors.most_common(limit)))
    months = Counter(git(repo, 'log', f'--since={since}', '--format=%ad',
                         '--date=format:%Y-%m').splitlines())
    write_table('monthly_momentum', ['month', 'commits'], sorted(months.items()))
    incidents = git(repo, 'log', f'--since={since}', '-i', '-E',
                    '--grep=revert|hotfix|emergency|rollback', '--format=%h%x09%s')
    write_table('firefighting', ['commit', 'subject'],
                (line.split('\t', 1) for line in incidents.splitlines()))


def default_files(repo):
    tracked = git(repo, 'ls-files', '-z').split('\0')
    return [repo / path for path in tracked
            if Path(path).suffix.lower() in SOURCE_SUFFIXES
            and not EXCLUDED_PARTS.intersection(part.lower() for part in Path(path).parts)]


def selected_files(repo, arguments):
    paths = [Path(argument).resolve() for argument in arguments] if arguments else default_files(repo)
    for path in paths:
        path.relative_to(repo)
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            raise ValueError(f'not a C/C++ source file: {path}')
    return list(dict.fromkeys(paths))


def analyze(lizard, paths):
    if not paths:
        return []
    output = subprocess.check_output(
        [lizard, '--csv', '--no-gitignore', '-i', '-1', *map(str, paths)], text=True
    )
    return [dict(path=Path(row[6]), name=row[7], line=int(row[9]),
                 ccn=int(row[1]), nloc=int(row[0]))
            for row in csv.reader(io.StringIO(output))]


def histories(repo, paths, since, churn, bugs, explicit):
    history = {repo: (churn, bugs)}
    owners = {}
    directory_owners = {}
    for path in paths:
        owner = repo
        if explicit:
            directory = path.parent
            if directory not in directory_owners:
                directory_owners[directory] = Path(git(directory, 'rev-parse', '--show-toplevel').strip())
            owner = directory_owners[directory]
        owners[path] = owner
        if owner not in history:
            history[owner] = (file_churn(owner, since), file_churn(owner, since, bugs=True))
    return history, owners


def report(repo, paths, functions, history, owners, limit):
    per_file = {}
    for path in paths:
        owner = owners[path]
        churn, bugs = history[owner]
        relative = str(path.relative_to(owner))
        per_file[path] = dict(commits=churn[relative], bugs=bugs[relative],
                              count=0, maximum=0, high=0)
    for function in functions:
        metrics = per_file[function['path']]
        metrics['count'] += 1
        metrics['maximum'] = max(metrics['maximum'], function['ccn'])
        metrics['high'] += function['ccn'] > 20
        function['commits'] = metrics['commits']
        function['risk'] = function['ccn'] * metrics['commits']
    files = sorted(per_file, key=lambda p: (-per_file[p]['maximum'] * per_file[p]['commits'],
                                           -per_file[p]['maximum'], str(p)))
    write_table('file_heatmap',
                ['max_ccn_x_commits', 'max_ccn', 'commits', 'bug_commits',
                 'functions', 'ccn_over_20', 'history_repo', 'file'],
                ((per_file[path]['maximum'] * per_file[path]['commits'],
                  per_file[path]['maximum'], per_file[path]['commits'],
                  per_file[path]['bugs'], per_file[path]['count'], per_file[path]['high'],
                  str(owners[path].relative_to(repo)), str(path.relative_to(repo))) for path in files))
    ranked = sorted(functions, key=lambda f: (-f['risk'], -f['ccn'], str(f['path']), f['line']))
    header = ['ccn_x_commits', 'ccn', 'commits', 'nloc', 'file', 'line', 'function']
    write_table('function_heatmap', header, function_rows(repo, ranked[:limit]))
    write_table('ccn_over_20', header,
                function_rows(repo, sorted((f for f in functions if f['ccn'] > 20),
                                          key=lambda f: (-f['ccn'], str(f['path']), f['line']))))
    write_table('summary', ['files', 'functions', 'ccn_over_10', 'ccn_over_20'],
                [(len(paths), len(functions), sum(f['ccn'] > 10 for f in functions),
                  sum(f['ccn'] > 20 for f in functions))])


def function_rows(repo, functions):
    return ((f['risk'], f['ccn'], f['commits'], f['nloc'],
             str(f['path'].relative_to(repo)), f['line'], f['name']) for f in functions)


def main():
    parser = argparse.ArgumentParser(
        description='Report git diagnosis, then C/C++ complexity times file commit count.',
        epilog='Defaults to tracked sources excluding generated fonts, builds, dependencies and submodules. '
               'Explicit files are relative to the current directory and may include submodule sources. '
               'Set LIZARD to a lizard executable; set HEATMAP_PYTHON to the Python interpreter.')
    parser.add_argument('--since', default='1 year ago', help='git history window (default: 1 year ago)')
    parser.add_argument('--limit', type=int, default=20, help='top hotspot rows (default: 20)')
    parser.add_argument('files', nargs='*')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    lizard = shutil.which(os.environ.get('LIZARD', 'lizard'))
    if not lizard:
        parser.error('lizard is required: install lizard or set LIZARD to its executable')
    repo = Path(git(Path(__file__).resolve().parent, 'rev-parse', '--show-toplevel').strip())
    paths = selected_files(repo, args.files)
    churn = file_churn(repo, args.since)
    bugs = file_churn(repo, args.since, bugs=True)
    write_table('configuration', ['repository', 'since', 'lizard'], [(repo, args.since, lizard)])
    print_diagnosis(repo, args.since, args.limit, churn, bugs)
    history, owners = histories(repo, paths, args.since, churn, bugs, bool(args.files))
    report(repo, paths, analyze(lizard, paths), history, owners, args.limit)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'heatmap: {error}', file=sys.stderr)
        sys.exit(1)

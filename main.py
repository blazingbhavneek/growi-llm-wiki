"""CLI entry point; command composition lives in :mod:`runner.cli`."""

from __future__ import annotations

from runner import cli as _cli

# Keep the existing import surface for callers that invoke command handlers
# directly, while keeping this module limited to CLI-to-runner delegation.
PROJECT_ROOT = _cli.PROJECT_ROOT
main = _cli.main
_settings = _cli._settings
open_project = _cli.open_project
_progress = _cli._progress
_report = _cli._report


def build_parser():
    """Return the legacy parser while preserving direct ``main`` patch points."""

    parser = _cli.build_parser()
    parse_args = parser.parse_args
    wrappers = {
        _cli.cmd_check: cmd_check,
        _cli.cmd_convert: cmd_convert,
        _cli.cmd_sync: cmd_sync,
        _cli.cmd_pull: cmd_pull,
        _cli.cmd_human: cmd_human,
        _cli.cmd_watch: cmd_watch,
        _cli.cmd_queue: cmd_queue,
        _cli.cmd_build: cmd_build,
        _cli.cmd_publish: cmd_publish,
        _cli.cmd_index: cmd_index,
        _cli.cmd_reset: cmd_reset,
        _cli.cmd_link: cmd_link,
    }

    def parse_with_compat(*args, **kwargs):
        parsed = parse_args(*args, **kwargs)
        parsed.fn = wrappers.get(parsed.fn, parsed.fn)
        return parsed

    parser.parse_args = parse_with_compat
    return parser


def _compat_call(function, args):
    """Keep monkeypatching ``main`` working for older direct callers."""

    old_settings, old_project = _cli._settings, _cli.open_project
    _cli._settings, _cli.open_project = _settings, open_project
    try:
        return function(args)
    finally:
        _cli._settings, _cli.open_project = old_settings, old_project


def cmd_check(args):
    return _compat_call(_cli.cmd_check, args)


def cmd_convert(args):
    return _compat_call(_cli.cmd_convert, args)


def cmd_sync(args):
    return _compat_call(_cli.cmd_sync, args)


def cmd_pull(args):
    return _compat_call(_cli.cmd_pull, args)


def cmd_human(args):
    return _compat_call(_cli.cmd_human, args)


def cmd_watch(args):
    return _compat_call(_cli.cmd_watch, args)


def cmd_queue(args):
    return _compat_call(_cli.cmd_queue, args)


def cmd_build(args):
    return _compat_call(_cli.cmd_build, args)


def cmd_publish(args):
    return _compat_call(_cli.cmd_publish, args)


def cmd_index(args):
    return _compat_call(_cli.cmd_index, args)


def cmd_reset(args):
    return _compat_call(_cli.cmd_reset, args)


def cmd_link(args):
    return _compat_call(_cli.cmd_link, args)


if __name__ == "__main__":
    raise SystemExit(main())

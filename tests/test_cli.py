

def test_every_cli_argument_has_help():
    """A flag with no help is a flag nobody can use without reading the source.

    Introspects the parser tree rather than scraping `--help` output: argparse wraps long help
    onto the following line, so a text scrape reports documented flags as bare (it flagged 47
    commands when the real number was 29). 74 leaf commands, and `--id` alone means three
    different things -- account, device, rollout -- so each says WHICH id it wants.
    """
    import argparse

    from openmv_ota.cli import build_parser

    def walk(parser, path):
        bad = []
        for a in parser._actions:
            if isinstance(a, argparse._SubParsersAction):
                for name, sub in a.choices.items():
                    bad += walk(sub, path + [name])
                continue
            if isinstance(a, argparse._HelpAction):
                continue
            if not a.help:
                bad.append("%s: %s" % (" ".join(path), "/".join(a.option_strings) or a.dest))
        return bad

    undocumented = walk(build_parser(), [])
    assert undocumented == [], "undocumented CLI arguments:\n  " + "\n  ".join(undocumented)

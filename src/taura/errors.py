class TauraError(Exception):
    """A usage or configuration error. The CLI exits with code 2."""

    exit_code = 2


class Refusal(Exception):
    """An operation refused because of a finding (e.g. publish over a hand edit). Exit code 1."""

    exit_code = 1

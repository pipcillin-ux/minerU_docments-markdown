"""Command dispatcher for section reasoning modes."""

from .adopt import adopt_mode
from .apply import apply_mode
from .collect import collect_mode
from .common import parse_args
from .report import report_mode
from .review import review_mode
from .summary import summary_mode


def main() -> int:
    args = parse_args()
    if args.mode == "collect":
        collect_mode(args)
        return 0
    if args.mode == "summary":
        return summary_mode(args)
    if args.mode == "review":
        return review_mode(args)
    if args.mode == "apply":
        return apply_mode(args)
    if args.mode == "adopt":
        return adopt_mode(args)
    return report_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())

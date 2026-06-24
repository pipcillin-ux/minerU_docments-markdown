"""Heading state helpers for H1-H6 section paths."""


def update_heading_stack(stack: list[str], level: int, heading_text: str) -> list[str]:
    if not 1 <= level <= 6:
        raise ValueError(f"Heading level must be between 1 and 6, got {level}.")
    updated = list(stack[:6])
    if len(updated) < 6:
        updated.extend([""] * (6 - len(updated)))
    updated[level - 1] = heading_text
    for index in range(level, 6):
        updated[index] = ""
    return updated


def heading_stack_path(stack: list[str]) -> list[str]:
    return [value for value in stack if value]

import argparse
from pathlib import Path

from pagefold.demo import identity_base, image, type_shifted, vacuum_copy

SCENARIOS = ("quartz", "cerulean", "umber", "stress-left", "stress-right")
IDENTITY_SCENARIOS = {
    "identity-base": identity_base,
    "identity-vacuum": lambda: vacuum_copy(identity_base()),
    "identity-type-shift": type_shifted,
}


def main():
    parser = argparse.ArgumentParser(description="生成 Pagefold 演练镜像")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--identity-only", action="store_true",
                        help="只生成两种身份对照样本（原件、VACUUM 件、类型漂移件）")
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    names = IDENTITY_SCENARIOS if args.identity_only else {
        **{name: (lambda n=name: image(n)) for name in SCENARIOS},
        **IDENTITY_SCENARIOS,
    }
    for name, builder in names.items():
        path = directory / (name + ".sqlite")
        with path.open("xb") as file:
            file.write(builder())
        print(path)


if __name__ == "__main__":
    main()

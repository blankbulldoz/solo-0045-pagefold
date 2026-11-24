import argparse
from pathlib import Path

from pagefold.demo import image
from examples.schema_cases import SCHEMAS, case_image


def main():
    parser = argparse.ArgumentParser(description="生成 Pagefold 演练镜像")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--with-cases", action="store_true",
                        help="同时在 schema-cases 子目录生成两组结构差异三方镜像")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for name in ("quartz", "cerulean", "umber", "stress-left", "stress-right"):
        path = args.directory / (name + ".sqlite")
        with path.open("xb") as file:
            file.write(image(name))
        print(path)
    if args.with_cases:
        target = args.directory / "schema-cases"
        target.mkdir(exist_ok=True)
        for name in SCHEMAS:
            path = target / (name + ".sqlite")
            with path.open("xb") as file:
                file.write(case_image(name))
            print(path)


if __name__ == "__main__":
    main()

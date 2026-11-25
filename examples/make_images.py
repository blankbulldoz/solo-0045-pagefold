import argparse
from pathlib import Path

from pagefold.demo import image


def main():
    parser = argparse.ArgumentParser(description="生成 Pagefold 演练镜像")
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("quartz", "cerulean", "umber", "stress-left", "stress-right",
                 "impact-base", "impact-left", "impact-right"):
        path = directory / (name + ".sqlite")
        with path.open("xb") as file:
            file.write(image(name))
        print(path)


if __name__ == "__main__":
    main()

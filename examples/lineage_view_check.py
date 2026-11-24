"""谱系网页渲染的独立冒烟检查（不走 HTTP 写入口，仅验证视图组装）。"""
import tempfile
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

from pagefold.attempts import rehearse
from pagefold.decisions import resolve
from pagefold.demo import lineage_image
from pagefold.images import register
from pagefold.plans import create_plan
from pagefold.releases import seal
from pagefold.server import make_server
from pagefold.store import Store


def main():
    with tempfile.TemporaryDirectory() as root:
        store = Store(Path(root) / "state.sqlite")
        with make_server(store, 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                base = register(store, lineage_image("gen1-base"), "一代祖本")
                left = register(store, lineage_image("gen1-left"), "一代左支")
                right = register(store, lineage_image("gen1-right"), "一代右支")
                plan = create_plan(store, {"base": base["id"], "left": left["id"],
                                           "right": right["id"]})
                conflict = next(i for i in plan["items"] if not i["automatic"])
                revision = resolve(store, plan["id"], 0, conflict["id"], "right")["revision"]
                rehearse(store, plan["id"], revision)
                certificate = seal(store, plan["id"], revision)
                with store.connect() as db:
                    candidate = db.execute(
                        "SELECT candidate FROM releases WHERE plan_id=?",
                        (plan["id"],)).fetchone()["candidate"]
                # 重新导入与封存候选逐字节相同的文件。
                register(store, candidate, "回填封存件")
                origin = f"http://127.0.0.1:{server.server_port}"

                lineage_html = urlopen(
                    f"{origin}/images/{certificate['candidate_digest']}/lineage").read().decode()
                for needle in ("来源谱系", "强证据", "弱证据", "含糊或缺失的来源边",
                               "正式计划中的角色复用", "封存输出"):
                    assert needle in lineage_html, f"谱系页缺少 {needle}"

                # 输入镜像应带计划角色边；C1 输出本身在一代不带角色边。
                base_html = urlopen(f"{origin}/images/{base['id']}/lineage").read().decode()
                assert "计划角色" in base_html and "祖本" in base_html, "祖本角色边未渲染"

                image_html = urlopen(f"{origin}/images/{base['id']}").read().decode()
                assert "/lineage" in image_html and "查看来源谱系" in image_html, "镜像页缺谱系入口"
                print("lineage-view ✓ 谱系页、角色边与镜像入口均正确渲染")
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()

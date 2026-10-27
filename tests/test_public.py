"""Offline checks: no WeChat login and no model calls."""

import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from wechat_group_ai import bot, bridge, memory


def config(group_id: str, name: str, members: list[str]) -> dict:
    return {
        "group_id": group_id, "group_name": name,
        "member_count": len(members), "member_ids": members,
        "owner_id": "owner", "bot_id": "bot",
    }


class FakeDB:
    wxid = "bot"

    def __init__(self, group: dict, source: str):
        self.group = group
        self.source = source

    def get_groups(self):
        return [self.group]

    def get_message_row(self, *_args, **_kwargs):
        return {"source": self.source}


class PublicChecks(unittest.TestCase):
    def test_config_accepts_one_arbitrary_group_and_rejects_duplicates(self):
        group = config("g1", "测试群", ["bot", "owner", "friend"])
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "groups.json"
            with patch.object(bot, "CONFIG_PATH", path):
                path.write_text(json.dumps({"groups": [group]}), encoding="utf-8")
                self.assertEqual(bot.load_configs(), [group])
                path.write_text(json.dumps({"groups": [group, group]}), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    bot.load_configs()

    def test_real_mention_and_pinned_group_checks(self):
        conf = config("g1", "测试群", ["bot", "owner", "friend"])
        group = {"username": "g1", "name": "测试群", "member_count": 3,
                 "members": [{"username": item} for item in conf["member_ids"]]}
        msg = {"sender_username": "friend", "type": "文本", "content": "@机器人 你好", "local_id": 1}
        real = FakeDB(group, "<msgsource><atuserlist>bot</atuserlist></msgsource>")
        fake = FakeDB(group, "<msgsource><atuserlist /></msgsource>")
        self.assertEqual(bot.checked_group(real, conf), group)
        self.assertEqual(bot.eligible_message(real, conf, group, msg), (True, "@机器人 你好"))
        self.assertEqual(bot.eligible_message(fake, conf, group, msg)[0], False)
        self.assertEqual(bridge.at_users("<msgsource><atuserlist>bot</atuserlist></msgsource>"), ("bot",))
        with self.assertRaises(RuntimeError):
            bot.checked_group(FakeDB({**group, "name": "别的群"}, ""), conf)

    def test_memory_is_group_scoped_and_credentials_stopped_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            con = memory.connect(pathlib.Path(directory) / "memory.sqlite3")
            conf = config("g1", "测试群", ["bot", "owner", "friend"])
            with patch.object(memory, "_model_text", return_value="ALLOW"):
                reply = memory.handle_command(con, ("remember", None, "小明说周五聚会"), conf, "friend", "小明")
            self.assertIn("已记住 #", reply)
            self.assertEqual(len(memory.list_memories(con, "g1")), 1)
            self.assertEqual(memory.list_memories(con, "g2"), [])
            with patch.object(memory, "_model_text") as model:
                rejected = memory.handle_command(con, ("remember", None, "验证码是123456"), conf, "friend", "小明")
                self.assertIn("未保存", rejected)
                model.assert_not_called()
            con.close()


if __name__ == "__main__":
    unittest.main()

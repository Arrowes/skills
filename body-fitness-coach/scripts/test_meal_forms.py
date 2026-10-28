"""Run with the private interpreter. No network calls or real food writes."""
import copy
import json
from pathlib import Path
import tempfile
from datetime import date, timedelta
from unittest.mock import patch

import coach
import meal_forms as forms
import install_meal_bridge


def check():
    original = coach.DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        coach.set_data_dir(Path(tmp))
        today = date.today()
        food = {"meal_type": "morning", "name": "测试食物", "amount": 100, "unit": "g", "ntr": {"cal": 100, "protein": 5, "carb": 15, "fat": 2}}
        prior = {"date": str(today - timedelta(days=1)), "foods": {"records": [food]}}
        history = {"res": {"days": [prior]}}
        config = {"chat_id": "chat-test", "meal_owner_id": "ou_test"}
        with patch.object(coach, "meta_set", side_effect=AssertionError("preview writes")):
            preview, _ = forms.build_form(config, history, today, persist=False)
        fields = preview["body"]["elements"][0]["elements"]
        assert sum(e["tag"] == "select_static" for e in fields) == 3
        assert sum(e["tag"] == "input" for e in fields) == 4
        assert all("bridge_token" not in str(e) for e in fields)
        assert forms.history_choices(history, today)["breakfast"][0]["foods"][0]["amount"] == 100
        other = {**food, "name": "另一食物"}
        reordered = {"res": {"days": [
            {"date": str(today - timedelta(days=2)), "foods": {"records": [food, other]}},
            {"date": str(today - timedelta(days=1)), "foods": {"records": [other, food]}},
        ]}}
        grouped = forms.history_choices(reordered, today)["breakfast"]
        assert len(grouped) == 1 and grouped[0]["count"] == 2
        varied = {"res": {"days": [{"date": str(today - timedelta(days=i + 1)), "foods": {"records": [{**food, "name": f"food-{i}"}]}} for i in range(7)]}}
        assert len(forms.history_choices(varied, today)["breakfast"]) == 5
        assert forms.snack_meal_type(9) == "morning-added"
        assert forms.snack_meal_type(14) == "noon-added"
        assert forms.snack_meal_type(22) == "night-added"
        long_foods = [{**food, "name": "带有冗长备注的测试食物（估算份量）"} for _ in range(5)]
        long_title = forms.choice_title(long_foods, 0)
        assert long_title.count("带有冗长备注的测试食物") == 5
        assert "等5项" not in long_title and "…" not in long_title
        breakfast = [{**food, "name": name} for name in ("茶叶蛋", "鲜牛奶", "香菇青菜包")]
        breakfast.sort(key=forms.breakfast_food_order)
        assert forms.choice_title(breakfast, 0) == "1 · 香菇青菜包＋牛奶＋茶叶蛋"
        assert not any(e["tag"] == "collapsible_panel" for e in fields)
        _, nonce = forms.build_form(config, history, today)
        assert forms.build_form(config, history, today)[1] == nonce
        def fresh_form():
            with coach.db() as conn:
                conn.execute("delete from meta where key like 'meal_card:%'")
            return forms.build_form(config, history, today)[1]
        event = {"nonce": nonce, "chat_id": "chat-test", "sender_id": "ou_test", "form_value": {"breakfast_choice": "0"}}
        feedback = {"meal": {"calories": 100, "protein": 5, "carbs": 15, "fat": 2}, "consumed": {"calories": 100, "protein": 5, "carbs": 15, "fat": 2}, "targets": {"calories": 2000, "protein": 100, "carbs": 250, "fat": 60}}
        writes = []
        def write(path, source):
            writes.append((json.loads(path.read_text(encoding="utf-8")), source))
            print(json.dumps({"feedback": feedback}))
            return 0
        with patch.object(coach, "load_config", return_value=config), patch.object(coach, "post_json", return_value=history), patch.object(coach, "write_food", side_effect=write) as writer:
            for change in ({"sender_id": "ou_other"}, {"chat_id": "other"}, {"nonce": "a" * 32}):
                try:
                    forms.submit({**event, **change})
                    raise AssertionError("invalid event accepted")
                except ValueError:
                    pass
            writer.assert_not_called()
            assert "早餐已记录" in forms.submit(event)
            assert "未重复写入" in forms.submit(event)
            assert len(writes) == 1
            assert writes[0][0]["foods"][0]["meal_type"] == "breakfast"
            assert writes[0][0]["foods"][0]["date"] == str(today)
            fresh = fresh_form()
            custom = forms.validate_foods([food], "breakfast", str(today))
            with patch.object(forms, "parse_custom", return_value=custom) as parse:
                forms.submit({**event, "nonce": fresh, "form_value": {"breakfast_choice": "0", "breakfast_text": "自定义测试"}})
                parse.assert_called_once()
            assert len(writes) == 2
            uncertain = fresh_form()
            with patch.object(coach, "write_food", side_effect=TimeoutError("unknown external outcome")):
                try:
                    forms.submit({**event, "nonce": uncertain})
                    raise AssertionError("timeout ignored")
                except TimeoutError:
                    pass
            retry = fresh_form()
            try:
                forms.submit({**event, "nonce": retry})
                raise AssertionError("uncertain meal resent from a new card")
            except ValueError as exc:
                assert "尚未确认" in str(exc)
            coach.meta_set(f"meal_uncertain:{today}:breakfast", "")
            fresh = fresh_form()
            with patch.object(forms, "parse_custom", side_effect=ValueError("invalid")):
                try:
                    forms.submit({**event, "nonce": fresh, "form_value": {"breakfast_choice": "0", "lunch_text": "not food"}})
                    raise AssertionError("unparsed food accepted")
                except ValueError:
                    pass
            assert len(writes) == 2  # No partial write when parsing another meal fails.
            recorded = copy.deepcopy(history)
            recorded["res"]["days"].append({"date": str(today), "foods": {"records": [food]}})
            with patch.object(coach, "post_json", return_value=recorded):
                assert "未重复写入" in forms.submit({**event, "nonce": fresh})
            assert len(writes) == 2
            snapshot = json.loads(coach.meta_get("meal_form:" + fresh))
            snapshot["date"] = str(today - timedelta(days=1))
            coach.meta_set("meal_form:" + fresh, json.dumps(snapshot))
            try:
                forms.submit({**event, "nonce": fresh})
                raise AssertionError("expired form accepted")
            except ValueError:
                pass
        extra_history = {"res": {"days": [{"date": str(today - timedelta(days=1)), "foods": {"records": [{**food, "name": "鲜牛奶", "unit": "ml"}, {**food, "name": "测试蛋白粉"}]}}]}}
        extra_card, extra_nonce = forms.build_form(config, extra_history, today)
        extra_fields = extra_card["body"]["elements"][0]["elements"]
        assert any(e["tag"] == "multi_select_static" and len(e["options"]) == 2 for e in extra_fields)
        with patch.object(coach, "load_config", return_value=config), patch.object(coach, "post_json", return_value=extra_history), patch.object(coach, "write_food", side_effect=write), patch.object(forms, "parse_custom", side_effect=AssertionError("preset consumed model tokens")):
            extra_event = {**event, "nonce": extra_nonce, "form_value": {"other_choices": ["protein", "milk"]}}
            assert "其他饮食已记录" in forms.submit(extra_event)
            extra_foods = writes[-1][0]["foods"]
            assert len(extra_foods) == 2
            assert {f["amount"] for f in extra_foods} == {35, 260}
            assert all(f["meal_type"] == forms.snack_meal_type(forms.datetime.now().hour) for f in extra_foods)
            written_before = len(writes)
            assert "未重复写入" in forms.submit(extra_event)
            assert len(writes) == written_before
            assert forms.build_form(config, extra_history, today)[1] != extra_nonce
        normalized = coach.normalize_food_meal_types([{"meal_type": "夜宵"}])
        assert normalized[0]["meal_type"] == "night-added"
        bridge = Path(tmp) / "bridge"
        (bridge / "dist").mkdir(parents=True)
        fake = 'var handlers = {};\nconst raw = {}; const formValue = raw?.action?.form_value;'
        (bridge / "dist/cli.js").write_text(fake, encoding="utf-8")
        assert install_meal_bridge.install(bridge)["state"] == "planned"
        assert (bridge / "dist/cli.js").read_text(encoding="utf-8") == fake
        assert install_meal_bridge.install(bridge, apply=True)["state"] == "installed"
        assert install_meal_bridge.install(bridge, apply=True)["state"] == "installed"
    coach.set_data_dir(original)
    print("meal forms: rendering, preview, owner/chat, date, choice, custom precedence, dedup, existing meals, parse failure, hook install OK")


if __name__ == "__main__":
    check()

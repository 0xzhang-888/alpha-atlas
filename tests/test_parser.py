import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.parser import parse_message


def test_format_one_basic():
    text = """🌟监控到新关注动态
你关注的用户: 合成观察者Observer ◎🔶(备注:合成观察者Observer ◎🔶) (https://x.com/ObserverDemo)
用户所属分组: SyntheticGroup
用户列表:
  • Synthetic Project e/acc (@ProjectDemo) (https://x.com/ProjectDemo)"""
    events = parse_message(text)
    assert len(events) == 1
    assert events[0].kol.startswith("合成观察者")
    assert events[0].project_handle == "ProjectDemo"
    assert events[0].follower_handle == 'ObserverDemo'


def test_format_one_multiple():
    text = """🌟监控到新关注动态
你关注的用户: KOL_A (https://x.com/kola)
用户所属分组: SyntheticGroup
用户列表:
  • Project One (@proj1) (https://x.com/proj1)
  • Project Two (@Proj2) (https://x.com/Proj2)"""
    events = parse_message(text)
    handles = {e.project_handle for e in events}
    assert handles == {"proj1", "Proj2"}


def test_format_two_basic():
    text = """合成观察者二 关注了 DemoProject
粉丝数:29 用户简介:Building demo tools. On @ethereum
https://x.com/demoproject"""
    events = parse_message(text)
    assert len(events) == 1
    assert events[0].kol == "合成观察者二"
    assert events[0].project_handle == "demoproject"
    assert events[0].bio == "Building demo tools. On @ethereum"
    assert events[0].follower_handle is None


def test_explicit_outgoing_handle_only():
    event = parse_message('@Early 关注了 Base\n用户简介:Base chain\nhttps://x.com/base')[0]
    assert event.follower_handle == 'Early'
    assert event.project_handle == 'base'
    display = parse_message('Early 关注了 Base\nhttps://x.com/base')[0]
    assert display.follower_handle is None


def test_format_two_profile_url_after_bio_links():
    text = "KOL 关注了 Demo\n用户简介:Built by https://x.com/Builder\nOn Ethereum\nhttps://x.com/Demo"
    event = parse_message(text)[0]
    assert event.project_handle == "Demo"
    assert event.bio == "Built by https://x.com/Builder\nOn Ethereum"


def test_irrelevant_message():
    text = "随便发点别的消息"
    assert parse_message(text) == []


def test_empty():
    assert parse_message("") == []


if __name__ == "__main__":
    test_format_one_basic()
    test_format_one_multiple()
    test_format_two_basic()
    test_irrelevant_message()
    test_empty()
    print("all parser tests passed")

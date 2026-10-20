import pytest

from civconvert.transforms import apply_transforms, has_cjk, strip_chinese


@pytest.mark.parametrize(
    "raw, expected",
    [
        # descriptions from invoice 8100800394
        ("Selfie Stick + Throwpod 快拍脚架自拍杆", "Selfie Stick + Throwpod"),
        ("Insta360 Mic Air 无线麦克风", "Insta360 Mic Air"),
        ('3-prong to 1/4"Adapter 相机支架', '3-prong to 1/4"Adapter'),
        ("Memory Card (128G) 内存卡", "Memory Card (128G)"),
        ("Panoramic camera(X5) 全景相机", "Panoramic camera(X5)"),
        ("Insta360 Flow2 Stabilizer 手持云台稳定器", "Insta360 Flow2 Stabilizer"),
        # other layouts
        ("Pivot Stand转向支架", "Pivot Stand"),
        ("Pivot Stand / 转向支架", "Pivot Stand"),
        ("Pivot Stand - 转向支架", "Pivot Stand"),
        ("Selfie Stick（快拍脚架）", "Selfie Stick"),
        ("Memory Card（128G）内存卡", "Memory Card(128G)"),
        ("Memory Card (128G 内存卡)", "Memory Card (128G)"),
        ("快拍脚架 Selfie Stick", "Selfie Stick"),
        ("【配件】Quick Release Mount", "Quick Release Mount"),
        ("ND Filter Set", "ND Filter Set"),
        ("无线麦克风", ""),
        ("", ""),
    ],
)
def test_strip_chinese(raw, expected):
    assert strip_chinese(raw) == expected


def test_strip_chinese_keeps_only_leading_english_by_default():
    assert strip_chinese("Pivot Stand 转向支架 Black") == "Pivot Stand"
    assert strip_chinese("Pivot Stand 转向支架 Black", mode="remove_all") == "Pivot Stand Black"


def test_has_cjk():
    assert has_cjk("Mic 麦克风") and not has_cjk("Mic Pro (Black)")


def test_apply_transforms_with_options():
    specs = ["strip", {"name": "regex_replace", "pattern": r"[^0-9.]", "replace": ""}]
    assert apply_transforms("  US$1,600.00 ", specs) == "1600.00"

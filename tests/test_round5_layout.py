"""Round 5: Safety stepper, collapsible sidebar, accessible menus and theme contrast.

Structure is checked here; behaviour (keyboard use, persistence, collapsing) is validated in a real browser.
Contrast uses the WCAG 2.x formula on the page's own theme tokens, for both themes.
"""
import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / 'src/meloshelf/presentation/web/index.html').read_text(encoding='utf-8')


def tokens(block: str) -> dict:
    found = re.findall(r'--([a-z0-9]+):(#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))(?![0-9a-fA-F])', block)
    # #abc means #aabbcc
    return {name: ('#' + ''.join(c * 2 for c in value[1:]) if len(value) == 4 else value) for name, value in found}


def theme_tokens():
    light = tokens(re.search(r'^:root\{([^}]*)\}', PAGE, re.M).group(1))
    dark = tokens(re.search(r':root\[data-theme="dark"\]\{([^}]*)\}', PAGE).group(1))
    return {'light': light, 'dark': dark}


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ── themes ──────────────────────────────────────────────────────────────────────

def test_dark_theme_tokens_match_between_the_system_and_the_explicit_selector():
    system = re.search(r'prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{([^}]*)\}', PAGE).group(1)
    assert tokens(system) == theme_tokens()['dark']


def test_text_colours_meet_the_normal_text_contrast_in_both_themes():
    for name, t in theme_tokens().items():
        for text, surface in (('ink', 'bg'), ('ink', 'sf'), ('mu', 'bg'), ('mu', 'sf'), ('mu', 'sf2'), ('ac', 'bg'),
                              ('ac', 'sf'), ('ok', 'sf'), ('ok', 'oks'), ('rv', 'rvs'), ('un', 'uns'), ('ac', 'acs')):
            ratio = contrast(t[text], t[surface])
            assert ratio >= 4.5, f'{name}: {text} on {surface} is {ratio:.2f}:1'


def test_the_text_on_accent_fills_meets_the_contrast_in_both_themes():
    for name, t in theme_tokens().items():
        assert contrast(t['on'], t['ac']) >= 4.5, f'{name}: on/ac is {contrast(t["on"], t["ac"]):.2f}:1'


def test_borders_and_focus_indicators_are_visible_against_their_surfaces():
    for name, t in theme_tokens().items():
        assert contrast(t['ac'], t['bg']) >= 3.0, name
    assert re.search(r':focus-visible\{[^}]*outline', PAGE), 'a visible keyboard focus indicator is defined'


# ── Safety stepper ──────────────────────────────────────────────────────────────

def test_the_safety_phases_are_an_ordered_list_with_centred_numbered_steps():
    safety = re.search(r'<section data-v="safety">(.*?)</section>', PAGE, re.S).group(1)
    match = re.search(r'<ol class="steps"[^>]*aria-label="([^"]+)"[^>]*>(.*?)</ol>', safety, re.S)
    assert match, 'the transaction phases must be an ordered list'
    assert 'planned' in match.group(1).lower()
    items = re.findall(r'<li[^>]*>(.*?)</li>', match.group(2), re.S)
    assert len(items) == 6
    for item in items:
        assert re.search(r'<span class="n"[^>]*>\d</span>', item) and re.search(r'<span class="t"[^>]*>[^<]+</span>', item)


def test_the_stepper_is_centred_in_its_row_and_each_label_under_its_number():
    css = re.search(r'\.steps\{[^}]*\}', PAGE).group(0)
    assert 'justify-content:center' in css
    item = re.search(r'\.steps li\{[^}]*\}', PAGE).group(0)
    assert 'flex-direction:column' in item and 'align-items:center' in item and 'text-align:center' in item


def test_the_whole_workflow_card_is_centred_not_only_the_steps():
    card = re.search(r'<div class="card workflow"[^>]*>', PAGE).group(0)
    assert 'text-align:center' in card


# ── collapsible sidebar ─────────────────────────────────────────────────────────

def test_the_sidebar_has_an_accessible_collapse_button_and_a_labelled_landmark():
    assert re.search(r'<nav id="mainNav" aria-label="Main"', PAGE)
    button = re.search(r'<button[^>]*id="navToggle"[^>]*>', PAGE).group(0)
    assert 'aria-expanded="true"' in button and 'aria-controls="mainNav"' in button and 'aria-label="' in button


def test_collapsing_keeps_the_link_names_available_to_assistive_technology():
    rule = re.search(r'body\.nav-collapsed nav a \.lbl[^{]*\{[^}]*\}', PAGE).group(0)
    assert 'clip' in rule and 'display:none' not in rule, 'labels are visually hidden, not removed'
    assert re.search(r'body\.nav-collapsed\{[^}]*grid-template-columns', PAGE)


def test_the_collapsed_state_is_remembered_without_depending_on_storage():
    assert re.search(r"try\{[^}]*localStorage\.(get|set)Item\('meloshelf\.nav'", PAGE)
    assert "nav-collapsed" in PAGE and "aria-expanded" in PAGE


def test_the_collapse_control_is_hidden_where_the_sidebar_becomes_a_top_bar():
    assert re.search(r'@media\(max-width:860px\)\{[^@]*\.nav-toggle\{display:none\}', PAGE)


# ── accessible popover menus ────────────────────────────────────────────────────

def test_the_theme_and_language_controls_are_menu_buttons_with_menus():
    for name in ('theme', 'lang'):
        button = re.search(rf'<button[^>]*id="{name}Btn"[^>]*>', PAGE).group(0)
        assert 'aria-haspopup="menu"' in button and 'aria-expanded="false"' in button
        assert f'aria-controls="{name}Menu"' in button
        menu = re.search(rf'<div[^>]*id="{name}Menu"[^>]*>(.*?)</div>', PAGE, re.S)
        assert menu and 'role="menu"' in menu.group(0) and 'hidden' in menu.group(0)
        assert re.search(r'role="menuitemradio"[^>]*aria-checked="(true|false)"', menu.group(1))


def test_the_menu_script_implements_the_expected_keyboard_behaviour():
    for key in ('ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape', 'Tab'):
        assert f"'{key}'" in PAGE, key
    assert "aria-expanded" in PAGE and '.focus()' in PAGE


def test_the_language_menu_lists_all_eight_languages_in_their_own_names():
    menu = re.search(r'<div[^>]*id="langMenu"[^>]*>(.*?)</div>', PAGE, re.S).group(1)
    for code, name in (('en', 'English'), ('fr', 'Français'), ('es', 'Español'), ('pt', 'Português'), ('ja', '日本語'),
                       ('zh-Hans', '简体中文'), ('ko', '한국어'), ('hi', 'हिन्दी')):
        assert f'data-lang="{code}"' in menu and name in menu, code

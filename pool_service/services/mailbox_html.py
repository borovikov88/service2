"""Restricted email markup for reading and composing. No remote resource is fetched server-side."""
import re
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import nh3
import tinycss2

MAX_MARKUP = 2_000_000
MAX_INLINE_URI_CHARS = 8 * 1024 * 1024
TEXT_TAGS = set("a b blockquote br code del div em font h1 h2 h3 h4 h5 h6 hr i li ol p pre s small span strong sub sup u ul".split())
READER_TAGS = TEXT_TAGS | set("article caption center col colgroup dd dl dt figure figcaption img section table tbody td th thead tfoot tr".split())
DROP_CONTENT = {"script", "style", "iframe", "object", "template", "svg", "math", "noscript", "title"}
TEXT_STYLE = set("font font-family font-size font-style font-weight line-height color background-color text-align text-decoration white-space".split())
READER_STYLE = TEXT_STYLE | set("""
    background display vertical-align width min-width max-width height min-height max-height
    border border-width border-style border-color border-radius border-top border-bottom border-left border-right
    border-collapse border-spacing padding padding-top padding-bottom padding-left padding-right
    margin margin-top margin-bottom margin-left margin-right overflow word-break letter-spacing table-layout
    list-style-type text-transform overflow-wrap
""".split())
ATTRIBUTES = {
    "*": {"dir", "style", "align"}, "a": {"href", "title"}, "font": {"face", "color", "size"},
    "ol": {"start"}, "li": {"value"},
}
READER_ATTRIBUTES = {**ATTRIBUTES, "*": ATTRIBUTES["*"] | {"class", "id", "bgcolor", "valign", "width", "height"},
                     "img": {"src", "alt", "title"}, "table": {"cellpadding", "cellspacing", "border"},
                     "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}, "col": {"span"}}
FRAME_CSS = """*{box-sizing:border-box}html,body{margin:0;padding:0}body{font:15px/1.45 Arial,sans-serif;
color:#23282e;background:white;overflow-wrap:anywhere}img{max-width:100%!important;height:auto}
table{max-width:100%!important}pre{white-space:pre-wrap}a{color:#1269b0}"""


def _style(css, allowed):
    result = []
    for rule in tinycss2.parse_declaration_list(css[:12000], skip_comments=True, skip_whitespace=True):
        if rule.type != "declaration" or rule.lower_name not in allowed:
            continue
        value = tinycss2.serialize(rule.value).strip()
        if not re.fullmatch(r"""[\w\s#.,%/'"()+-]{1,240}""", value):
            continue
        if any(t.type not in {"ident", "string", "number", "dimension", "percentage", "hash", "whitespace", "literal"}
               and not (t.type == "function" and t.lower_name in {"rgb", "rgba", "hsl", "hsla"}
                        and all(a.type in {"number", "percentage", "dimension", "whitespace", "literal"}
                                for a in t.arguments)) for t in rule.value):
            continue
        result.append(f"{rule.lower_name}:{value}" + (" !important" if rule.important else ""))
    return ";".join(result)


def _stylesheet(rules, *, media=True):
    result = []
    for rule in rules:
        if rule.type == "qualified-rule":
            selector = tinycss2.serialize(rule.prelude).strip()
            if re.fullmatch(r"[\w\s.*#,:>+~-]{1,320}", selector):
                style = _style(rule.content, READER_STYLE)
                if style:
                    result.append(selector + "{" + style + "}")
        elif media and rule.type == "at-rule" and rule.lower_at_keyword == "media" and rule.content:
            condition = tinycss2.serialize(rule.prelude).strip()
            if re.fullmatch(r"[\w\s():.,-]{1,160}", condition):
                nested = _stylesheet(tinycss2.parse_rule_list(rule.content), media=False)
                if nested:
                    result.append("@media " + condition + "{" + nested + "}")
    return "".join(result)


class _LetterStyles(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_style = False
        self.css = []
        self.budget = 65536
        self.body_style = ""

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self.in_style = True
        elif tag == "body":
            attrs = dict(attrs)
            self.body_style = _style((attrs.get("style") or "") + ";background-color:" + (attrs.get("bgcolor") or ""), READER_STYLE)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style and self.budget:
            chunk = data[:self.budget]
            self.budget -= len(chunk)
            self.css.append(chunk)


def _clean(markup, *, reader=False, image=None):
    def attribute(tag, key, value):
        if key == "style":
            return _style(value, READER_STYLE if reader else TEXT_STYLE) or None
        if tag == "img" and key == "src":
            return image(value) if image else None
        if key == "href":
            try:
                url = urlsplit(value)
                if url.scheme.lower() in ("mailto", "tel"):
                    return value if url.path and not url.netloc else None
                if url.scheme.lower() not in ("http", "https") or not url.hostname or url.username is not None:
                    return None
            except ValueError:
                return None
        return value

    return nh3.clean(markup, tags=READER_TAGS if reader else TEXT_TAGS, clean_content_tags=DROP_CONTENT,
                     attributes=READER_ATTRIBUTES if reader else ATTRIBUTES, attribute_filter=attribute,
                     set_tag_attribute_values={"a": {"target": "_blank"}},
                     url_schemes={"https", "http", "mailto", "tel", "cid", "data"}, url_relative="deny")


def _image_source(value):
    if len(value) <= 4096 and not any(c.isspace() or ord(c) < 32 for c in value):
        try:
            url = urlsplit(value)
            if url.scheme.lower() == "cid" and url.path and not url.netloc:
                return value
            if url.scheme.lower() == "https" and url.hostname and url.username is None:
                return value
        except ValueError:
            pass
    return None


def clean_letter(markup):
    if not markup or len(markup) > MAX_MARKUP:
        return ""
    styles = _LetterStyles()
    try:
        styles.feed(markup)
        styles.close()
    except (ValueError, AssertionError):
        pass
    css = _stylesheet(tinycss2.parse_stylesheet("\n".join(styles.css)))
    html = _clean(markup, reader=True, image=_image_source)
    if not html.strip():
        return ""
    if styles.body_style:
        html = '<div style="' + escape(styles.body_style, quote=True) + '">' + html + "</div>"
    return (f"<style>{css}</style>" if css else "") + html


def clean_authored(markup):
    return _clean(markup) if markup and len(markup) <= MAX_MARKUP else ""


def from_plain(text):
    return "<p>" + escape(text).replace("\r\n", "\n").replace("\n", "<br>") + "</p>" if text else ""


def has_text(fragment):
    # Input has already passed the HTML parser. A cached stylesheet or a nonbreaking space is not content.
    text = re.sub(r"<style>.*?</style>|<[^>]+>", "", fragment, flags=re.S)
    return bool(unescape(text).strip(" \t\n\r\xa0\u200b\ufeff"))


def letter_document(markup, inline_images, *, show_external=False):
    external = False
    remaining = MAX_INLINE_URI_CHARS
    visible_image = False

    def image_source(value):
        nonlocal external, remaining, visible_image
        value = _image_source(value)
        if value and value[:4].lower() == "cid:":
            uri = inline_images.get(unquote(value[4:]).strip("<>"))
            if uri and len(uri) <= remaining:
                remaining -= len(uri)
                visible_image = True
                return uri
            return None
        if value:
            external = True
            if show_external:
                visible_image = True
                return value
        return None

    # Markup was normalised by clean_letter, but treat stored data as untrusted at the rendering boundary.
    styles = _LetterStyles()
    styles.feed(markup)
    css = _stylesheet(tinycss2.parse_stylesheet("\n".join(styles.css)))
    fragment = _clean(markup, reader=True, image=image_source)
    if not visible_image and not has_text(fragment):
        return "", external
    images = "data: https:" if show_external else "data:"
    policy = f"default-src 'none'; style-src 'unsafe-inline'; img-src {images}; form-action 'none'; base-uri 'none'"
    document = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
                '<meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<style>{FRAME_CSS}{css}</style></head><body>{fragment}</body></html>")
    return document, external

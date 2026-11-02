"""The "Brief reels" tab: a creator pastes one brief for a whole kind of reel
(niche, how it is told, how it looks) and every reel made from it follows it.

``split_brief`` turns the pasted text into its three parts. ``generate_script``
writes a reel's script from them: the whole voice-over first, as one flowing
piece (so it reads as one thought, not a row of captions), then it is cut into
scenes and each scene gets a picture that shows what it MEANS, drawn in the
brief's look (``Reel.custom_style``). Reels from a brief are told calmly, like a
story (reel_render's ``story``)."""

from __future__ import annotations

import re
from collections.abc import Callable

from app.services import reel_script as rs
from app.services.reel_script import ScriptError
from app.services.tts import LANGUAGES, sentences

MAX_BRIEF = 8000  # characters of a pasted brief
MAX_PART = 3000  # characters of each of its parts
MAX_STYLE_WORDS = 70  # the look is put before every picture prompt: keep it short
DEFAULT_DURATION_S = 75


def scene_count(duration_s: int) -> int:
    """One picture for about 13 seconds (6 for 75 s, at most 8; past ~2.5
    minutes one every 20 s): the script is one piece, so the pictures need not
    follow it sentence by sentence, and each one takes a minute to draw."""
    return max(3, min(8, round(duration_s / 13)), round(duration_s / 20))


# ---------------------------------------------------------------------------
# The brief itself
# ---------------------------------------------------------------------------
def split_brief(content: str, *, model: str | None = None) -> dict[str, str]:
    """{"niche", "rules", "visual_style"} of a pasted brief. The visual style is
    one English paragraph for the image model: only what the pictures look like,
    since naming what to avoid ("no photorealism") makes it draw that."""
    text = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {"niche": text, "rules": text, "visual_style": text},
        "required": ["niche", "rules", "visual_style"],
    }
    messages = [
        {
            "role": "system",
            "content": "You read a creator's brief for a kind of short vertical video and "
            "split it into parts. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": "Split this brief into three parts, in English.\n"
            "- niche: what the videos are about (topics, audience), 1 to 3 sentences.\n"
            "- rules: how a script is written and told: the story structure, the narrator's "
            "voice and tone, what to do and what never to do. Keep the creator's own points; "
            "leave out anything about pictures, colours or camera.\n"
            f"- visual_style: ONE line of at most {MAX_STYLE_WORDS} words, comma-separated "
            "phrases like an image generator's style prompt, starting with the drawing medium "
            '(never a sentence like "The visual style is"): the technique, the colour '
            "palette, the light and mood, the kind of characters, the symbols that recur. "
            "Only what the pictures look like: never what to avoid, never video, animation, "
            'camera moves, aspect ratio or a reference video. Example: "Dark hand-drawn 2D '
            "editorial cartoon illustration, inked linework, muted sepia and charcoal "
            "palette, aged paper texture, moody low-key light, expressive symbolic "
            'characters, recurring symbols of clocks and chains".\n\n'
            f"The brief:\n{content}",
        },
    ]
    raw = rs._chat(
        messages, schema, temperature=0.3, num_predict=1200,
        num_ctx=max(4096, len(content) // 2 + 2000), model=model,
    )  # fmt: skip
    if not isinstance(raw, dict):
        raise rs.BadReplyError("The writer sent back something unreadable. Try again.")
    parts = {k: rs._clean(raw.get(k))[:MAX_PART] for k in ("niche", "rules", "visual_style")}
    if not parts["rules"] or not parts["visual_style"]:
        raise rs.BadReplyError("The brief could not be split. Try again.")
    if len(parts["visual_style"].split()) > 1.5 * MAX_STYLE_WORDS:
        parts["visual_style"] = _shorter_style(parts["visual_style"], model)
    return parts


def _shorter_style(style: str, model: str | None) -> str:
    """The look as a short comma-separated line (it came back as a long text);
    as it was if the writer can't."""
    text = {"type": "string"}
    messages = [
        {
            "role": "system",
            "content": "You write image generator style prompts. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": f"Rewrite this as ONE line of at most {MAX_STYLE_WORDS} words: "
            "comma-separated phrases starting with the drawing medium, then the technique, "
            "palette, light and mood, characters and recurring symbols. No sentences, no "
            f'"will be", nothing about what to avoid.\n\n{style}',
        },
    ]
    try:
        raw = rs._chat(
            messages, {"type": "object", "properties": {"style": text}, "required": ["style"]},
            temperature=0.2, num_predict=300, model=model,
        )  # fmt: skip
    except ScriptError:
        return style
    short = rs._clean(raw.get("style")) if isinstance(raw, dict) else ""
    return short if short and len(short.split()) <= 1.5 * MAX_STYLE_WORDS else style


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------
def _script_messages(
    brief: dict, topic: str, platform: str, language: str, duration_s: int, n: int,
    words: int, description: str,
) -> list[dict]:  # fmt: skip
    p = rs.PLATFORMS[platform]
    lang = LANGUAGES[language].prompt_name
    each = max(8, round(words / n))
    system = f"""You write voice-over scripts for short vertical videos ({p.label}), from the creator's brief. Reply with JSON only.
The creator's brief for this kind of video:
- Niche: {brief["niche"]}
- How to write and tell it: {brief["rules"]}
Rules:
- script: the whole voice-over as ONE flowing piece told straight to the viewer, in {lang} only.{rs.LANGUAGE_NOTES.get(language, "")}
- Exactly {n} paragraphs, separated by a blank line, one for each picture: about {each} words each ({round(words * 0.9)} to {round(words * 1.1)} in all). {_roles(n)}
- One strong idea about human psychology, society or the reality of life runs through the whole script, built up paragraph by paragraph. Each sentence leads into the next and adds something new: never repeat a point, no filler. Show people and moments, not slogans.
- Never give advice or orders ("you should", "stop chasing", "embrace"), never a moral lecture, never generic motivation: the viewer is left to think, not told what to do.
- Short, meaningful spoken sentences in simple everyday words; a calm, serious voice. No greeting ("hello friends"), no made-up names, no emojis, hashtags, labels or stage directions: only the words the narrator says.
- call_to_action: one short spoken line in {lang} to {p.call_to_action}. It is read right after the script, so never put it in the script.
- hooks: 3 alternative opening lines for the first paragraph, in {lang}, each a different way in (a question, a bold statement, a situation).
- title: in {lang}. caption: the post caption in {lang}, under 150 characters.
- hashtags: 5 relevant hashtags in English, lowercase, without the # sign.
- music: {rs.MUSIC_RULE}
Audience: {p.audience}"""  # noqa: E501
    user = f"Length: {duration_s} seconds (about {words} words)."
    if topic:
        user = f"Topic: {topic}\n{user}"
    else:
        user = (
            "No topic was given: invent one original concept that fits the niche, with one "
            f"strong idea.\n{user}"
        )
    if description:
        user += f"\nThe creator's note for this video, follow it closely:\n{description}"
    user += f"\nReminder: the whole script in {lang}."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _roles(n: int) -> str:
    """What each of the ``n`` paragraphs does (the brief's story shape)."""
    middle = "Paragraph 3" if n <= 5 else f"Paragraphs 3 to {n - 2}"
    if n < 4:
        return "Paragraph 1 is the hook, the last one is the powerful closing thought."
    return (
        "Paragraph 1 is the hook: a powerful psychological question or statement (it may be "
        "shorter). Paragraph 2 is a relatable real-life situation told concretely: a person, "
        f"a place, a moment. {middle}: the hidden truth about human behaviour, one deeper "
        "layer in each, each with a concrete everyday example. "
        + (f"Paragraph {n - 1}: the emotional or philosophical depth of it. " if n > 4 else "")
        + f"Paragraph {n} is the closing thought: short, powerful, and open."
    )


def _script_schema() -> dict:
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "title": text,
            "hooks": {"type": "array", "items": text, "minItems": 3, "maxItems": 3},
            "script": text,
            "call_to_action": text,
            "caption": text,
            "hashtags": {"type": "array", "items": text, "minItems": 3, "maxItems": 6},
            "music": rs.music_schema(),
        },
        "required": ["title", "hooks", "script", "call_to_action", "caption", "hashtags", "music"],
    }


def paragraphs(script: str) -> list[str]:
    parts = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", script or "")]
    parts = [p for p in parts if p]
    if len(parts) <= 1:  # one line per paragraph, with no blank line between
        parts = [re.sub(r"\s+", " ", p).strip() for p in (script or "").split("\n")]
        parts = [p for p in parts if p]
    return parts


def split_scenes(script: str, n: int) -> list[str]:
    """The script as ``n`` scenes of about the same number of words. A script
    with exactly ``n`` paragraphs is cut where they end; otherwise its sentences
    are grouped, preferring to cut at the end of a paragraph."""
    paras = paragraphs(script)
    if len(paras) == n:
        return paras
    items = []  # (sentence, words, ends a paragraph)
    for para in paras:
        pieces = sentences(para) or [para]
        items += [(s, len(s.split()), k == len(pieces) - 1) for k, s in enumerate(pieces)]
    m = len(items)
    n = min(n, m)
    if n == 0:
        return []
    total = [0]
    for _, w, _ in items:
        total.append(total[-1] + w)
    ideal = total[-1] / n
    penalty = (0.35 * ideal) ** 2  # cutting in the middle of a paragraph
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for j in range(1, n + 1):
        for i in range(j, m + 1):
            cut = 0 if i == m or items[i - 1][2] else penalty
            for k in range(j - 1, i):
                c = cost[j - 1][k] + (total[i] - total[k] - ideal) ** 2 + cut
                if c < cost[j][i]:
                    cost[j][i], back[j][i] = c, k
    spans, i = [], m
    for j in range(n, 0, -1):
        spans.append((back[j][i], i))
        i = back[j][i]
    return [" ".join(items[x][0] for x in range(a, b)) for a, b in reversed(spans)]


def _pictures(brief: dict, scenes: list[str], topic: str, *, model: str | None) -> dict:
    """{"setting", "characters", "scenes": [{"image_prompt", "characters",
    "crowd"}]}: a picture for each scene that shows what it means."""
    parts = "\n".join(f"Scene {i + 1}: {s}" for i, s in enumerate(scenes))
    text = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {
            "setting": text,
            "characters": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {"name": text, "look": text},
                    "required": ["name", "look"],
                },
            },
            "scenes": {
                "type": "array",
                "minItems": len(scenes),
                "maxItems": len(scenes),
                "items": {
                    "type": "object",
                    "properties": {
                        "image_prompt": text,
                        "characters": {"type": "array", "items": text, "maxItems": 2},
                        "crowd": {"type": "boolean"},
                    },
                    "required": ["image_prompt", "characters", "crowd"],
                },
            },
        },
        "required": ["setting", "characters", "scenes"],
    }
    messages = [
        {
            "role": "system",
            "content": "You plan the pictures of a short vertical video from its voice-over. "
            "Reply with JSON only. The pictures are drawn in this look, which is added "
            f"later: {brief['visual_style']}",
        },
        {
            "role": "user",
            "content": f"The voice-over of a video about {topic or brief['niche']}, in "
            f"{len(scenes)} scenes, each shown with one picture:\n{parts}\n\n"
            "- setting: in English, the world all the pictures share (place, era, "
            "surroundings), never the characters.\n"
            "- characters: the one or two recurring figures (at most 3), each with a short "
            "name in English letters (e.g. The Man) and their look in English, written in "
            "full once: age, build, face, clothes and colours. The same figure in every "
            "picture they are in.\n"
            "- scenes: for each scene one image_prompt in English: ONE concrete moment that "
            "shows what the scene MEANS, as a symbol or a visual metaphor (money, clocks, "
            "graves, masks, mirrors, chains, empty chairs, shadows...), never a plain "
            "decoration of the words. Say who does what, their pose and expression, where, "
            "and the lighting; vertical composition; call a character by name only (the "
            "look is added). Describe only what is in the picture, never how it is made. "
            "No text or letters, never a collage or split screen.\n"
            "  characters: the names seen in that picture (2 or fewer); crowd: true only "
            "if other unnamed people are seen too.",
        },
    ]
    raw = rs._chat(
        messages, schema, temperature=0.6,
        num_predict=450 * len(scenes) + 600, num_ctx=max(4096, 450 * len(scenes) + 2500),
        model=model,
    )  # fmt: skip
    if not isinstance(raw, dict) or not isinstance(raw.get("scenes"), list):
        raise rs.BadReplyError("The writer's pictures came back unreadable. Try again.")
    return raw


def generate_script(
    brief: dict,
    topic: str,
    platform: str,
    language: str,
    duration_s: int,
    description: str = "",
    voice: str | None = None,
    *,
    model: str | None = None,
    on_step: Callable[[str], None] | None = None,
) -> dict:
    """A script (as reel_script.generate_script's) for a reel of ``duration_s``
    seconds from ``brief`` ({"niche", "rules", "visual_style"})."""
    step = on_step or (lambda _: None)
    n = scene_count(duration_s)
    words = round(
        rs.script_words(rs.aim_s(duration_s), language, n, voice, True) - rs.CALL_TO_ACTION_WORDS
    )
    messages = _script_messages(brief, topic, platform, language, duration_s, n, words, description)
    num_predict = max(2000, 15 * duration_s + 500)
    raw = None
    for attempt in (1, 2):  # a small model sometimes gets stuck repeating itself
        try:
            raw = rs._chat(
                messages, _script_schema(), temperature=0.8, num_predict=num_predict,
                num_ctx=max(4096, num_predict + 2500 + len(brief["rules"]) // 2), model=model,
            )  # fmt: skip
            if not isinstance(raw, dict) or not rs._clean(raw.get("script")):
                raise rs.BadReplyError("The script came back empty. Try again.")
            break
        except rs.BadReplyError:
            if attempt == 2:
                raise
    texts = [rs._spoken(rs._clean(t, drop_latin_brackets=language != "en"))
             for t in split_scenes(raw["script"], n)]  # fmt: skip
    scenes = [{"narration": t} for t in texts if t]
    if not scenes:
        raise rs.BadReplyError("The script came back empty. Try again.")
    cta = rs.call_to_action(raw, language)
    step("Checking its length")
    rs.fit_length(scenes, language, duration_s, voice, cta, story=True, model=model)
    step("Planning the pictures")
    plan = _pictures(brief, [sc["narration"] for sc in scenes], topic, model=model)
    drawn = plan["scenes"]
    for i, sc in enumerate(scenes):
        item = drawn[i] if i < len(drawn) and isinstance(drawn[i], dict) else {}
        sc.update(image_prompt=item.get("image_prompt"), characters=item.get("characters"),
                  crowd=item.get("crowd"))  # fmt: skip
    script = rs.normalize_script(
        {**raw, "setting": plan.get("setting"), "characters": plan.get("characters"),
         "scenes": scenes},  # fmt: skip
        topic=topic or raw.get("title") or brief["niche"][:120],
        language=language, scenes=len(scenes), with_call_to_action=False,
    )  # fmt: skip
    rs.add_call_to_action(script["scenes"], cta)
    if language == "hi":
        step("Putting every word in Hindi")
        rs.hindi_only(script, model=model)
    step("Checking the pictures' words")
    rs.english_picture_texts(script, model=model)
    return script


class BriefMissing(ScriptError):
    """The reel's brief was deleted: it can't be written again from it."""

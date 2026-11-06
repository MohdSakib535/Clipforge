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
import unicodedata
from collections.abc import Callable

from app.core.logging import get_logger
from app.services import reel_script as rs
from app.services import tts
from app.services.reel_render import END_GAP_S, STORY_GAP_S, STORY_SPEED
from app.services.reel_script import ScriptError
from app.services.tts import LANGUAGES, sentences

log = get_logger(__name__)

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
    nouns = {"type": "array", "items": text, "maxItems": 15}
    schema = {
        "type": "object",
        "properties": {"niche": text, "rules": text, "visual_style": text, "symbols": nouns},
        "required": ["niche", "rules", "visual_style", "symbols"],
    }
    messages = [
        {
            "role": "system",
            "content": "You read a creator's brief for a kind of short vertical video and "
            "split it into parts. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": "Split this brief into three parts, in English, and list its symbols.\n"
            "- niche: what the videos are about (topics, audience), 1 to 3 sentences.\n"
            "- rules: how a script is written and told: the story structure, the narrator's "
            "voice and tone, what to do and what never to do. Keep the creator's own points; "
            "leave out anything about pictures, colours or camera.\n"
            f"- visual_style: ONE line of at most {MAX_STYLE_WORDS} words, comma-separated "
            "phrases like an image generator's style prompt, starting with the drawing medium "
            '(never a sentence like "The visual style is"): the technique, the colour '
            "palette, the light and mood, the kind of characters. Only how the pictures are "
            "drawn, never the objects or symbols in them (each picture gets its own), never "
            "what to avoid, never video, animation, camera moves, aspect ratio or a "
            'reference video. Example: "Dark hand-drawn 2D editorial cartoon illustration, '
            "inked linework, muted sepia and charcoal palette, aged paper texture, moody "
            'low-key light, expressive symbolic characters".\n'
            "- symbols: the objects or symbols the brief says its pictures show, each ONE "
            "English noun (money, clock, grave); [] if it names none.\n\n"
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
    symbols = raw.get("symbols") if isinstance(raw.get("symbols"), list) else []
    parts["visual_style"] = without_symbols(parts["visual_style"], symbols)
    return parts


# A look's phrase that lists symbols ("symbolic imagery of money, clocks...").
_SYMBOL_PHRASE = re.compile(
    r"\bsymbols\b|\bsymbolic (?:imagery|representations?|objects?|elements?|motifs?)\b",
    re.IGNORECASE,
)


def _phrases(style: str) -> list[str]:
    """A look's comma-separated phrases (also cut at dashes and semicolons),
    not cut inside brackets: "muted palette (sepia, charcoal)" stays one."""
    out, current, depth = [], "", 0
    for ch in style:
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        if depth == 0 and ch in ",;–—":
            out.append(current)
            current = ""
        else:
            current += ch
    out.append(current)
    return [p.strip().removeprefix("and ").strip(" .") for p in out if p.strip(" .")]


def _stem(word: str) -> str:
    word = word.casefold()
    if word.endswith("ss"):
        return word
    if word.endswith("es") and len(word) > 4 and word[-3] in "sxzh":
        return word[:-2]
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def without_symbols(style: str, symbols: list) -> str:
    """The look without the phrases naming the brief's symbols: the look goes
    before every picture prompt, so money, clocks, graves, masks, mirrors and
    chains came out in every picture, scattered all over. The picture planner
    reads them in the brief and puts one where it carries a scene's meaning.
    The look as it was if nothing would be left."""
    keys = {_stem(w) for s in symbols if isinstance(s, str) for w in re.findall(r"[A-Za-z]+", s)}
    kept = [
        p for p in _phrases(style)
        if not _SYMBOL_PHRASE.search(p)
        and not keys & {_stem(w) for w in re.findall(r"[A-Za-z]+", p)}
    ]  # fmt: skip
    return ", ".join(kept) if kept else style


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
            "palette, light and mood and characters. No sentences, no objects or symbols, no "
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
    words: int, description: str, earlier: list[str] = (),
) -> list[dict]:  # fmt: skip
    p = rs.PLATFORMS[platform]
    lang = LANGUAGES[language].prompt_name
    each = max(8, round(words / n))
    system = f"""You write voice-over scripts for short vertical videos ({p.label}), following the creator's brief. Reply with JSON only.
{_brief_block(brief)}
Rules:
- The brief comes first: follow its story structure, tone, narrator's voice and everything it says never to do.
- script: the whole voice-over as ONE flowing piece told straight to the viewer, in {lang} only.{rs.LANGUAGE_NOTES.get(language, "")}
- Exactly {n} paragraphs, separated by a blank line, one for each picture: about {each} words each ({round(words * 0.9)} to {round(words * 1.1)} in all). {_roles(n)}
- One clear idea or story runs through the whole script, built up paragraph by paragraph. Each sentence leads into the next and adds something new: never repeat a point, no filler. Show concrete people and moments, not slogans.
- Short, meaningful spoken sentences in simple everyday words. No greeting ("hello friends") unless the brief asks for one; no emojis, hashtags, labels or stage directions: only the words the narrator says.
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
        if earlier:  # the creator's own topic wins; without one, a new idea
            done = "\n".join(f"- {t}" for t in earlier)
            user += (
                "\nVideos already made from this brief: pick an idea none of them had, never "
                f"one of these again:\n{done}"
            )
    if description:
        user += f"\nThe creator's note for this video, follow it closely:\n{description}"
    user += f"\nReminder: the whole script in {lang}."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _brief_block(brief: dict) -> str:
    """The brief for a prompt: as the creator pasted it (``content``; the split
    parts lose their exact words and examples), then its parts, which the
    creator may have edited since."""
    parts = f"- Niche: {brief['niche']}\n- How to write and tell it: {brief['rules']}"
    content = (brief.get("content") or "").strip()
    if not content:
        return f"The creator's brief for this kind of video:\n{parts}"
    return (
        "The creator's brief, as they wrote it (its notes on how the pictures look are for "
        f"the pictures, not the script):\n<<<\n{content}\n>>>\n"
        "In short (the creator may have edited these: where they differ from the brief "
        f"above, follow these):\n{parts}"
    )


def _roles(n: int) -> str:
    """What the ``n`` paragraphs do: the story shape the brief gives, opened by
    a hook and closed by an ending."""
    if n < 3:
        return "Paragraph 1 is the hook, the last one is the ending."
    middle = "paragraph 2" if n == 3 else f"paragraphs 2 to {n - 1}"
    return (
        "Spread the story structure the brief describes over the paragraphs, in its order: "
        "its opening is paragraph 1, the hook that stops the scroll in the first 3 seconds "
        f"(it may be shorter); its ending is paragraph {n}, short and strong; its steps "
        f"between fill {middle}, one new step in each. If the brief gives no structure: a "
        "hook, a build-up one step per paragraph, an ending."
    )


def _review(
    brief: dict, texts: list[str], language: str, *, model: str | None
) -> list[str]:  # fmt: skip
    """The editor: the paragraphs checked against the brief, and the ones that
    break it (a rule of the brief, a repeated point, filler, a greeting, a
    weak hook or ending) written again, each about as long as it was so the
    length still fits. Best effort: the paragraphs as they were if it fails."""
    n = len(texts)
    if not n:
        return texts
    lang = LANGUAGES[language].prompt_name
    listed = "\n".join(f"Paragraph {i + 1}: {t}" for i, t in enumerate(texts))
    text = {"type": "string"}
    item = {
        "type": "object",
        "properties": {"problem": text, "text": text},
        "required": ["problem", "text"],
    }
    paragraphs = {"type": "array", "items": item, "minItems": n, "maxItems": n}
    schema = {
        "type": "object",
        "properties": {"paragraphs": paragraphs},
        "required": ["paragraphs"],
    }
    messages = [
        {
            "role": "system",
            "content": "You edit voice-over scripts for short vertical videos: you check a "
            "script against the creator's brief and fix only what breaks it. Reply with JSON "
            f"only.\n{_brief_block(brief)}",
        },
        {
            "role": "user",
            "content": f"The script, in {lang}, one paragraph per picture:\n{listed}\n\n"
            "Read every sentence against each rule of the brief, one by one. If the brief "
            "forbids advice, lectures or telling the viewer what to do, a sentence that tells "
            "them what to do, remember, feel or believe breaks it, in any language (like "
            '"remember...", "next time you...", "you should...", "learn to..."): say the '
            "same thing as an observation or a question to think about instead.\n"
            "For each paragraph, in order:\n"
            "- problem: empty if it is fine; otherwise what is wrong, in a few English words: "
            "it breaks something the brief asks for or forbids, repeats an earlier point, is "
            "filler, or greets the viewer; paragraph 1 is a weak hook that would not stop the "
            f"scroll; paragraph {n} is a weak ending.\n"
            "- text: the paragraph exactly as it is when it is fine; otherwise written again "
            f"to fix only that, in {lang}, with about the same number of words, the same "
            f"meaning and the same place in the story.{rs.LANGUAGE_NOTES.get(language, '')}",
        },
    ]
    words = sum(len(t.split()) for t in texts)
    num_predict = max(1500, 4 * words + 60 * n)  # Devanagari takes many tokens
    try:
        raw = rs._chat(
            messages, schema, temperature=0.3, num_predict=num_predict,
            num_ctx=max(4096, num_predict + len(messages[0]["content"]) // 2 + len(listed) + 500),
            model=model,
        )  # fmt: skip
    except ScriptError:
        return texts
    got = raw.get("paragraphs") if isinstance(raw, dict) else None
    if not isinstance(got, list) or len(got) != n:
        return texts
    out = list(texts)
    for i, (old, item) in enumerate(zip(texts, got, strict=True)):
        if not isinstance(item, dict) or not rs._clean(item.get("problem")):
            continue
        new = rs._spoken(rs._clean(item.get("text"), drop_latin_brackets=language != "en"))
        # About as long as before: the length was fitted to the paragraphs.
        if new and new != old and 0.7 <= len(new.split()) / len(old.split()) <= 1.4:
            out[i] = new
            log.info("brief_review_fixed", extra={"paragraph": i + 1, "problem": item["problem"]})
    return out


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


# Words the planner wants written in a picture ("files labelled 'Next Steps'"):
# the image model draws them garbled. The thing stays, without its words. A
# single quote only right after "labelled" & co.: elsewhere it is an apostrophe.
_LABELLED = re.compile(
    r"\s*,?\s*\b(?:labell?ed|marked|titled|reading|that reads|which reads|saying|that says|"
    r"with the (?:words?|text|letters?))\s*:?\s*['‘\"“][^'’\"”]{1,80}['’\"”](?=[\s.,;:!?)]|$)",
    re.IGNORECASE,
)
_DOUBLE_QUOTED = re.compile(r"\s*[\"“][^\"”]{1,80}[\"”]")


def without_written_words(prompt: str) -> str:
    out = _DOUBLE_QUOTED.sub("", _LABELLED.sub("", prompt))
    return re.sub(r"\s+([,.;])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()


# Where a reel's viewers live, by its language, and their money: its people,
# places and things, unless the brief or the script says otherwise (a Hindi reel
# came out in 19th-century Europe when the brief named no place, then in an
# Indian village with dollar notes).
VIEWERS_PLACE = {"hi": ("India", "Indian rupee notes and coins")}


# A brief that forbids advice or lectures ("No advice", "Avoid offering advice,
# lecturing"). The paragraph editor still let lines like "यात्रा को महसूस करें"
# (feel the journey) through, so such briefs get a check sentence by sentence.
_NO_ADVICE = re.compile(
    r"\b(?:no|avoid\w*|never|don'?t|do not|without)\b[^.\n]{0,40}?"
    r"\b(?:advice|advis\w*|lectur\w*|preach\w*|moralis\w*|moraliz\w*|"
    r"tell(?:ing)? (?:the )?(?:viewers?|people|audience) what)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?।])\s+")


def forbids_advice(brief: dict) -> bool:
    return bool(_NO_ADVICE.search(f"{brief.get('rules') or ''}\n{brief.get('content') or ''}"))


def _nfc(words: str) -> frozenset[str]:
    return frozenset(unicodedata.normalize("NFC", w) for w in words.split())


# How a Hindi sentence gives an order: a polite verb (करें, रखिए, अपनाएं, आइए)
# or a familiar one (करो) closing a clause, "-ना चाहिए" (should) or "मत" (don't).
# The code finds them, the model only says them again: asked to judge, it
# called "याद रखें" no advice one time and plain statements advice the next.
_HI_ORDER_ENDS = ("ें", "एं", "एँ", "िए", "िये", "इए", "इये")
_HI_FAMILIAR = _nfc("करो रखो देखो सोचो सीखो समझो बनो छोड़ो जाओ आओ चलो बचो मानो जानो सुनो पहचानो जियो")
# Words that end like an order but aren't one ("में" in, "हमें" to us, "के लिए"
# for; "चाहिए" alone is "needs": only "-ना चाहिए" is "should").
_HI_NOT_ORDERS = _nfc("में हमें तुम्हें उन्हें जिन्हें इन्हें किन्हें किए किये दिए दिये जिए पिए गए चाहिए")
# A story's opener ("सोचिए, ..." imagine) asks the viewer to picture something, not to act.
_HI_OPENERS = _nfc("सोचिए सोचो देखिए देखो सुनिए सुनो आइए")
_HI_QUESTION = _nfc(
    "क्या क्यों कैसे कैसा कैसी कौन कब कहाँ कहां किसने किसको किसका किसकी किसके कितना कितनी कितने"
)
_HI_WORD = re.compile(r"[ऀ-ॣॱ-ॿ]+")  # not ।, ॥ or digits
_CLAUSE = re.compile(r"[,;:!।.…–—-]")


def order_word(sentence: str) -> str | None:
    """The word that gives the viewer an order in a Hindi sentence, if any. A
    question is never one (asking the viewer to think is what the brief
    wants), but an order before it is: "अगली बार विचार कीजिए; क्या यह आभार है?"."""
    text = unicodedata.normalize("NFC", sentence).strip()
    clauses = _CLAUSE.split(text)
    if text.rstrip("\"”’' ").endswith("?"):
        # Only what comes before the question (its question word).
        asks = [n for n, c in enumerate(clauses) if set(_HI_WORD.findall(c)) & _HI_QUESTION]
        clauses = clauses[: asks[0]] if asks else []
    words = [w for c in clauses for w in _HI_WORD.findall(c)]
    for k, word in enumerate(words):
        if word == "मत" and k + 1 < len(words):
            return word
        if word == "चाहिए" and k > 0 and words[k - 1].endswith(("ना", "नी", "ने")):
            return word
    for n, clause in enumerate(clauses):
        found = _HI_WORD.findall(clause)
        if not found:
            continue
        last = found[-1]
        if last in _HI_NOT_ORDERS or last.endswith(("लिए", "लिये")):
            continue
        if n == 0 and len(found) <= 2 and last in _HI_OPENERS:
            continue
        if last in _HI_FAMILIAR or last.endswith(_HI_ORDER_ENDS):
            return last
    return None


def _no_orders(
    brief: dict, texts: list[str], language: str, *, model: str | None
) -> list[str]:  # fmt: skip
    """For a Hindi script from a brief that forbids advice: each sentence that
    gives the viewer an order (order_word) said again as an observation or a
    question. A new one that still gives an order is not used. Best effort:
    the paragraphs as they were if it fails."""
    if language != "hi" or not forbids_advice(brief):
        return texts
    split = [[s for s in _SENTENCE.split(t) if s.strip()] for t in texts]
    flat = [s for para in split for s in para]
    asked = [(i, w) for i, s in enumerate(flat) if (w := order_word(s))]
    if not asked:
        return texts
    lang = LANGUAGES[language].prompt_name
    listed = "\n".join(f'{k + 1}. {flat[i]}  (the order: "{w}")' for k, (i, w) in enumerate(asked))
    many = {"type": "array", "items": {"type": "string"}, "minItems": len(asked),
            "maxItems": len(asked)}  # fmt: skip
    schema = {"type": "object", "properties": {"sentences": many}, "required": ["sentences"]}
    messages = [
        {
            "role": "system",
            "content": "You rewrite voice-over sentences that give advice. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": "The creator's videos never give advice: the viewer is left to think, "
            f"never told what to do. Each of these sentences, in {lang}, tells the viewer what "
            f"to do (the word in brackets gives the order):\n{listed}\n\n"
            "Write each one again: the same thought as an observation about people and life, "
            "or as a question that leaves the viewer thinking, never an order or advice; in "
            f"{lang}, about as long, fitting where it stands in the voice-over."
            f"{rs.LANGUAGE_NOTES.get(language, '')}",
        },
    ]
    num_predict = max(800, 5 * sum(len(flat[i].split()) for i, _ in asked) + 40 * len(asked))
    try:
        raw = rs._chat(
            messages, schema, temperature=0.4, num_predict=num_predict,
            num_ctx=max(4096, num_predict + len(listed) + 1000), model=model,
        )  # fmt: skip
    except ScriptError:
        return texts
    got = raw.get("sentences") if isinstance(raw, dict) else None
    if not isinstance(got, list) or len(got) != len(asked):
        return texts
    fixed = list(flat)
    for (i, word), new in zip(asked, got, strict=True):
        old = flat[i]
        new = rs._spoken(rs._clean(new, drop_latin_brackets=True))
        if new and not order_word(new) and 0.5 <= len(new.split()) / len(old.split()) <= 2:
            fixed[i] = new
            log.info("brief_order_rephrased", extra={"order": word})
    out, k = [], 0
    for para in split:
        out.append(" ".join(fixed[k : k + len(para)]))
        k += len(para)
    return out


def _pictures(
    brief: dict, scenes: list[str], topic: str, *, language: str, model: str | None
) -> dict:
    """{"setting", "characters", "scenes": [{"image_prompt", "characters",
    "crowd"}]}: a picture for each scene that shows what it means."""
    parts = "\n".join(f"Scene {i + 1}: {s}" for i, s in enumerate(scenes))
    place, money = VIEWERS_PLACE.get(language) or (None, None)
    where = (
        f"The viewers are in {place}: unless the brief or the voice-over says otherwise, "
        f"the people, their faces and clothes, the places and everyday things are from "
        f"{place}, and money is {money}.\n"
        if place
        else ""
    )
    content = (brief.get("content") or "").strip()
    notes = (
        "\nThe creator's brief, for what it says about the pictures (their symbols, the kind "
        f"of characters):\n<<<\n{content}\n>>>"
        if content
        else ""
    )
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
            f"later: {brief['visual_style']}{notes}",
        },
        {
            "role": "user",
            "content": f"The voice-over of a video about {topic or brief['niche']}, in "
            f"{len(scenes)} scenes, each shown with one picture:\n{parts}\n\n{where}"
            "- setting: in English, the world all the pictures share (place, era, "
            "surroundings), never the characters. No writing anywhere in it, as the image "
            "model writes nonsense letters: no shop signs, signboards, posters, banners or "
            "newspapers; quiet lanes, homes, rooms, courtyards, fields, or a market seen "
            "without its signs.\n"
            "- characters: the one or two recurring figures (at most 3), each with a short "
            "name in English letters (e.g. The Man) and their look in English, written in "
            "full once: age, build, face, clothes and colours. The same figure in every "
            "picture they are in.\n"
            "- scenes: for each scene one image_prompt in English: ONE concrete moment that "
            "shows what the scene MEANS: what happens in it, or, when the scene is a thought "
            "rather than an event, a visual metaphor for it, never a plain decoration of the "
            "words. A symbol the brief names only where it carries the scene's meaning: at "
            "most one main symbol in a picture, never many scattered around (a clean picture "
            "with one clear idea). Say who does what, their pose and expression, where, "
            "and the lighting; vertical composition; call a character by name only (the "
            "look is added). Describe only what is in the picture, never how it is made. "
            "No text or letters, never a collage or split screen.\n"
            "  characters: the names seen in that picture (2 or fewer); crowd: true only "
            "if other unnamed people are seen too.",
        },
    ]
    raw = rs._chat(
        messages, schema, temperature=0.6,
        num_predict=450 * len(scenes) + 600,
        num_ctx=max(4096, 450 * len(scenes) + 2500 + len(content) // 2),
        model=model,
    )  # fmt: skip
    if not isinstance(raw, dict) or not isinstance(raw.get("scenes"), list):
        raise rs.BadReplyError("The writer's pictures came back unreadable. Try again.")
    return raw


def _read_aloud(language: str, voice: str | None) -> Callable[[list[str]], float]:
    """How long narrations take as a reel from a brief reads them: read out by
    its voice (Kokoro reads ~6x faster than real time), each line once, with
    the pauses reel_render puts between the scenes. Counting words missed by
    10% for a Hindi script full of commas (93 s for 75). Counted if the voice
    can't be made."""
    voice = voice or tts.default_voice(language)
    done: dict[str, float] = {}

    def seconds(texts: list[str]) -> float:
        try:
            for t in texts:
                if t not in done:
                    samples, rate = tts.synthesize(
                        t, language, voice, speed=STORY_SPEED, by_sentence=True
                    )
                    done[t] = len(samples) / rate
        except tts.TTSError:
            return rs.spoken_seconds(texts, language, voice, story=True)
        return sum(done[t] for t in texts) + STORY_GAP_S * (len(texts) - 1) + END_GAP_S

    return seconds


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
    earlier: list[str] = (),
) -> dict:
    """A script (as reel_script.generate_script's) for a reel of ``duration_s``
    seconds from ``brief`` ({"niche", "rules", "visual_style"}); ``earlier``:
    the titles of reels already made from it (with no topic, a new idea)."""
    step = on_step or (lambda _: None)
    n = scene_count(duration_s)
    words = round(
        rs.script_words(rs.aim_s(duration_s), language, n, voice, True) - rs.CALL_TO_ACTION_WORDS
    )
    messages = _script_messages(
        brief, topic, platform, language, duration_s, n, words, description, earlier
    )
    num_predict = max(2000, 15 * duration_s + 500)
    brief_chars = len(brief["rules"]) + len(brief.get("content") or "")
    raw = None
    for attempt in (1, 2):  # a small model sometimes gets stuck repeating itself
        try:
            raw = rs._chat(
                messages, _script_schema(), temperature=0.8, num_predict=num_predict,
                num_ctx=max(4096, num_predict + 2500 + brief_chars // 2), model=model,
            )  # fmt: skip
            if not isinstance(raw, dict) or not rs._clean(raw.get("script")):
                raise rs.BadReplyError("The script came back empty. Try again.")
            break
        except rs.BadReplyError:
            if attempt == 2:
                raise
    texts = [rs._spoken(rs._clean(t, drop_latin_brackets=language != "en"))
             for t in split_scenes(raw["script"], n)]  # fmt: skip
    texts = [t for t in texts if t]
    if not texts:
        raise rs.BadReplyError("The script came back empty. Try again.")
    step("Checking it against the brief")
    texts = _review(brief, texts, language, model=model)
    texts = _no_orders(brief, texts, language, model=model)
    scenes = [{"narration": t} for t in texts]
    cta = rs.call_to_action(raw, language)
    step("Checking its length")
    rs.fit_length(
        scenes, language, duration_s, voice, cta, story=True, model=model,
        measure=_read_aloud(language, voice),
    )  # fmt: skip
    step("Planning the pictures")
    plan = _pictures(
        brief, [sc["narration"] for sc in scenes], topic, language=language, model=model
    )
    drawn = plan["scenes"]
    for i, sc in enumerate(scenes):
        item = drawn[i] if i < len(drawn) and isinstance(drawn[i], dict) else {}
        prompt = item.get("image_prompt")
        if isinstance(prompt, str):
            prompt = without_written_words(prompt)
        sc.update(image_prompt=prompt, characters=item.get("characters"),
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

// "Brief reels": paste one brief for a whole kind of reel (what the videos are
// about, how a script is told, how the pictures look) and make reels that all
// follow it. A nested Alpine component inside clipforge(), built on
// reelMaker() (reels.js): the script editor, music, video, bucket copy and
// polling are that component's; only the brief and the list of reels are
// this one's (its reels are the ones with a brief_id, apart from "Create a reel").
const BRIEF_EXAMPLE = `# My channel: Dark Human Psychology

## What my videos are about
Short Facebook videos about human psychology, greed, money, death and the hidden reality of everyday life. Each video explains ONE strong idea about why people behave the way they do.

## How the story is told
- Start with a powerful psychological question or statement (the hook).
- Then a relatable real-life situation: a person, a place, a moment.
- Then the hidden truth about human behaviour, one layer deeper each time.
- End with a short, thought-provoking closing line.
- No advice, no lectures, no generic motivation.

## Narration
A deep, calm, serious voice. Short, simple sentences in natural conversational language. Dark philosophical tone.

## How the pictures look
Dark, hand-drawn 2D editorial cartoon illustrations in a muted vintage palette (sepia, charcoal, faded olive). Expressive human characters. Symbols such as money, clocks, graves, masks, mirrors and chains. The pictures must explain the meaning of the narration, not just decorate it.`;

const CHATGPT_PROMPT = `I make short vertical videos with an app. It turns a "brief" into a script (voice-over) and drawn pictures, so the brief describes my WHOLE kind of video, not one story.

Please write my brief about: [write your channel / niche here, e.g. "motivational stories for students"].

Write it in English, under 2000 characters, with these four parts:
1. What my videos are about (niche, topics, audience)
2. How each story is told (the hook, the build-up, the ending, what to avoid)
3. The narration (voice, tone, short or long sentences)
4. How the pictures look (drawing style, colours, characters, recurring symbols)

Here is an example of the format:

${BRIEF_EXAMPLE}`;

function briefReels() {
  const base = reelMaker();
  const DEFAULT_LENGTH_S = 75; // the brief's videos run 1 to 1.5 minutes
  return {
    ...base,
    briefs: [],
    briefId: "", // the brief picked
    adding: false, // the "paste a new brief" box is open (it is, while there is no brief)
    newBrief: { content: "" },
    briefMax: 8000,
    briefExample: BRIEF_EXAMPLE,
    scriptText: "", // the open reel's whole script: its scenes' narrations, a blank line between
    briefBusy: "", // "saving" | "splitting" while the writer reads a brief
    edit: { niche: "", rules: "", visual_style: "" }, // the picked brief, being changed

    async initReels() {
      await base.initReels.call(this); // options, music, listeners, and loadReels() below
      this.form.duration_s = DEFAULT_LENGTH_S;
      // The better writer (12B) is picked here; the fast one can still be chosen.
      const writer = this.options.brief_script_model_default;
      if (this.options.script_models.some((m) => m.key === writer)) this.form.script_model = writer;
      // The one script box follows the script (a hook picked, a saved script...).
      this.$watch("draft", () => this.syncScriptText());
      await this.loadBriefs();
    },

    // As brief_script.scene_count: a picture for about 13 s (at most 8), past ~2.5 minutes one every 20 s.
    sceneCount(d) {
      return Math.max(3, Math.min(8, Math.round(d / 13)), Math.round(d / 20));
    },

    // ----- The script in one box: a blank line starts a new picture -----
    paragraphsOf(text) {
      return text.split(/\n\s*\n/).map((p) => p.replace(/\s+/g, " ").trim()).filter(Boolean);
    },
    syncScriptText() {
      const joined = this.draft ? this.draft.scenes.map((sc) => sc.narration).join("\n\n") : "";
      // Not while the box already says it (that would undo a blank line being typed).
      if (this.paragraphsOf(this.scriptText).join("\n\n") !== joined) this.scriptText = joined;
    },
    // Each paragraph is a scene, and keeps the picture description of the old
    // paragraph it shares most words with (so splitting or joining one doesn't
    // shift the pictures after it). A paragraph cut in two gives both halves a copy.
    applyScriptText() {
      const paras = this.paragraphsOf(this.scriptText).slice(0, this.options.max_scenes);
      if (!this.draft || !paras.length) return;
      const old = this.draft.scenes;
      const bag = (t) => new Set(t.toLowerCase().split(/\s+/).filter((w) => w.length > 2)); // not the small words
      const was = old.map((sc) => bag(sc.narration)); // before any scene changes
      this.draft.scenes = paras.map((narration, j) => {
        const mine = bag(narration);
        let best = Math.min(j, old.length - 1), top = 0.3; // by place, unless one matches clearly
        was.forEach((theirs, i) => {
          let shared = 0;
          mine.forEach((w) => theirs.has(w) && shared++);
          const score = shared / Math.max(1, mine.size); // how much of this paragraph was in that one
          if (score > top) [best, top] = [i, score];
        });
        return { ...JSON.parse(JSON.stringify(old[best])), narration }; // keeps the key order
      });
    },

    // ----- What to write in a brief -----
    useExample() {
      this.newBrief.content = BRIEF_EXAMPLE;
    },
    async copyChatGPTPrompt() {
      try {
        await navigator.clipboard.writeText(CHATGPT_PROMPT);
        this.notice = "Prompt copied. Paste it in ChatGPT, then paste its answer here.";
      } catch (e) {
        this.error = "Could not copy: select the example above and copy it by hand.";
      }
    },

    // ----- Briefs -----
    async sendJSON(method, url, body) {
      const r = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        const detail = j.error?.details?.[0];
        throw new Error(detail ? `${j.error.message}: ${detail.loc?.slice(1).join(" ")} ${detail.msg}` : j.error?.message || `HTTP ${r.status}`);
      }
      return r.status === 204 ? null : r.json();
    },
    brief() {
      return this.briefs.find((b) => b.id === this.briefId) || null;
    },
    briefName(id) {
      return this.briefs.find((b) => b.id === id)?.name || "Brief";
    },
    async loadBriefs() {
      try {
        const r = await fetch("/briefs");
        if (r.ok) this.briefs = await r.json();
      } catch (e) {}
      this.pickBrief(this.brief() ? this.briefId : this.briefs[0]?.id || "");
    },
    pickBrief(id) {
      this.briefId = id;
      const b = this.brief();
      this.edit = b
        ? { niche: b.niche, rules: b.rules, visual_style: b.visual_style }
        : { niche: "", rules: "", visual_style: "" };
    },
    briefDirty() {
      const b = this.brief();
      return !!b && Object.keys(this.edit).some((k) => this.edit[k].trim() !== b[k]);
    },
    startNewBrief() {
      this.newBrief = { content: "" };
      this.adding = true;
    },
    async saveNewBrief() {
      this.briefBusy = "saving";
      try {
        const b = await this.sendJSON("POST", "/briefs", { content: this.newBrief.content });
        this.briefs = [b, ...this.briefs];
        this.adding = false;
        this.pickBrief(b.id);
        this.notice = "Brief saved. Check its three parts, then write a reel from it.";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.briefBusy = "";
      }
    },
    async saveBriefParts() {
      this.briefBusy = "saving";
      try {
        const b = await this.sendJSON("PUT", `/briefs/${this.briefId}`, {
          niche: this.edit.niche, rules: this.edit.rules, visual_style: this.edit.visual_style,
        });
        this.briefs = this.briefs.map((x) => (x.id === b.id ? b : x));
        this.pickBrief(b.id);
        this.notice = "Brief saved. New reels follow it; reels made before keep their pictures.";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.briefBusy = "";
      }
    },
    async splitAgain() {
      if (!confirm("Read the pasted brief again? The three parts are replaced, including what you changed.")) return;
      this.briefBusy = "splitting";
      try {
        const b = await this.sendJSON("POST", `/briefs/${this.briefId}/split`);
        this.briefs = this.briefs.map((x) => (x.id === b.id ? b : x));
        this.pickBrief(b.id);
      } catch (e) {
        this.error = e.message;
      } finally {
        this.briefBusy = "";
      }
    },
    async deleteBrief() {
      const b = this.brief();
      if (!b || !confirm(`Delete the brief "${b.name}"?\n\nIts reels are kept, but can't be written again.`)) return;
      try {
        await this.sendJSON("DELETE", `/briefs/${b.id}`);
        this.briefs = this.briefs.filter((x) => x.id !== b.id);
        this.pickBrief(this.briefs[0]?.id || "");
      } catch (e) {
        this.error = e.message;
      }
    },

    // ----- Reels from the picked brief -----
    async loadReels() {
      try {
        const r = await fetch("/briefs/reels/all");
        if (r.ok) this.reels = await r.json();
      } catch (e) {}
    },
    async createReel() {
      if (!this.brief()) return;
      this.creating = true;
      try {
        const f = this.form;
        const reel = await this.sendJSON("POST", `/briefs/${this.briefId}/reels`, {
          topic: f.topic, platform: f.platform, language: f.language, voice: f.voice || null,
          duration_s: f.duration_s, description: f.description, script_model: f.script_model,
        });
        f.topic = "";
        f.description = "";
        this.reels = [reel, ...this.reels];
        this.open(reel);
      } catch (e) {
        this.error = e.message;
      } finally {
        this.creating = false;
      }
    },
    // Open a reel: the brief it came from is the one picked.
    open(reel) {
      base.open.call(this, reel);
      if (reel.brief_id && reel.brief_id !== this.briefId && this.briefs.some((b) => b.id === reel.brief_id)) {
        this.pickBrief(reel.brief_id);
      }
    },
  };
}

// "Local videos": everything ClipForge keeps on this computer - videos
// (uploaded or from a link) with the clips and reels made from them, reels
// made from a topic, music - with the disk space each uses, to clean up.
// A nested Alpine component inside clipforge(), so it shares its toasts
// (error/notice), the open video (to reset that screen) and helpers.
function localData() {
  return {
    local: null, // GET /data/local: { videos, reels, music, models }
    localLoading: false,
    localError: "",
    localFilter: { text: "", sort: "newest" }, // sort: "newest" | "biggest"
    deleting: null, // id of what is being deleted

    initLocal() {
      // Loaded when the tab is opened (sizes are measured on the disk each time).
      this.$watch("mode", (m) => m === "local" && this.loadLocal());
    },

    async loadLocal() {
      this.localLoading = true;
      this.localError = "";
      try {
        const r = await fetch("/data/local");
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        this.local = await r.json();
      } catch (e) {
        this.localError = "Could not load the list: " + e.message;
      } finally {
        this.localLoading = false;
      }
    },

    // ----- Disk space -----
    sum(items, field) {
      return (items || []).reduce((n, x) => n + (x[field] || 0), 0);
    },
    usage() {
      const l = this.local || {};
      const parts = {
        videos: this.sum(l.videos, "video_bytes"),
        clips: this.sum(l.videos, "clips_bytes"),
        reels: this.sum(l.reels, "bytes"),
        music: this.sum(l.music, "bytes"),
      };
      return { ...parts, total: Object.values(parts).reduce((a, b) => a + b, 0) };
    },
    modelsBytes() {
      return Object.values(this.local?.models || {}).reduce((a, b) => a + b, 0);
    },

    // ----- Videos -----
    videoBytes(v) {
      return v.video_bytes + v.clips_bytes;
    },
    copies(v) {
      return this.local.videos.filter((x) => x.filename === v.filename).length;
    },
    shownVideos() {
      const text = this.localFilter.text.trim().toLowerCase();
      const list = this.local.videos.filter((v) => !text || v.filename.toLowerCase().includes(text));
      if (this.localFilter.sort === "biggest") list.sort((a, b) => this.videoBytes(b) - this.videoBytes(a));
      return list;
    },
    videoLink(v, download = false) {
      return `/videos/${v.id}/source${download ? "?download=1" : ""}`;
    },

    async deleteVideo(v) {
      const what = v.clips
        ? `the video (${this.fmtBytes(v.video_bytes)}), its ${v.clips} clip${v.clips === 1 ? "" : "s"} and reels (${this.fmtBytes(v.clips_bytes)}) and its transcript`
        : `the video (${this.fmtBytes(v.video_bytes)}) and its transcript`;
      if (!confirm(`Delete "${v.filename}"?\n\nThis deletes ${what} from this computer. ` +
        "It can't be undone. Copies in your bucket are kept.")) return;
      const res = await this.removeLocal(v.id, `/data/videos/${v.id}`);
      if (!res) return;
      if (this.video?.id === v.id) await this.reset(); // "Clip a video" had it open
      this.notice = `"${v.filename}" deleted: ${this.fmtBytes(res.bytes_freed)} freed.`;
    },

    async deleteVideoClips(v) {
      if (!confirm(`Delete the ${v.clips} clip${v.clips === 1 ? "" : "s"} and reels made from "${v.filename}" ` +
        `(${this.fmtBytes(v.clips_bytes)})?\n\nThe video and its transcript stay, so new clips are quick to make.`)) return;
      const res = await this.removeLocal(v.id, `/data/videos/${v.id}/clips`);
      if (!res) return;
      if (this.job?.video_id === v.id) {
        Object.assign(this, { job: null, clips: [], step: "trim" }); // its review screen is empty now
      }
      this.notice = `${res.clips} clip${res.clips === 1 ? "" : "s"} deleted: ${this.fmtBytes(res.bytes_freed)} freed.`;
    },

    // ----- Reels from a topic, music -----
    async deleteLocalReel(r) {
      if (!confirm(`Delete the reel "${r.title}" (${this.fmtBytes(r.bytes)})?\n\nIts pictures, voice and video are deleted. Your music is kept.`)) return;
      if (!(await this.removeLocal(r.id, `/reels/${r.id}`))) return;
      window.dispatchEvent(new CustomEvent("clipforge:reels-changed"));
      this.notice = `Reel deleted: ${this.fmtBytes(r.bytes)} freed.`;
    },
    async deleteLocalMusic(t) {
      if (!confirm(`Delete "${t.filename}" (${this.fmtBytes(t.bytes)}) from your music?\n\n` +
        "Reels already made keep their sound.")) return;
      if (!(await this.removeLocal(t.id, `/audio/${t.id}`))) return;
      await this.loadTracks();
      this.notice = `"${t.filename}" deleted.`;
    },

    // DELETE ``url``, then reload the list. Returns the answer (or {}), or null on an error.
    async removeLocal(id, url) {
      this.deleting = id;
      try {
        const r = await fetch(url, { method: "DELETE" });
        const j = r.status === 204 ? {} : await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
        return j;
      } catch (e) {
        this.error = e.message;
        return null;
      } finally {
        this.deleting = null;
        await this.loadLocal();
      }
    },

    async deleteEverything() {
      await this.deleteAllData();
      await this.loadLocal();
    },
  };
}

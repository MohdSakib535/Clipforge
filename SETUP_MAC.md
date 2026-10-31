# ClipForge — Mac pe start karne ke steps

Ye guide naye Mac pe ClipForge chalane ke liye hai: purana **"Clip a video"** flow
(video upload karke clips banana) aur naya **"Create a reel"** (AI se script, pictures,
voice aur reel video).

Setup **ek baar** karna hai (Step 1–4). Phir Step 5 se pehli baar chalao. Uske baad roz
ka kaam sirf Step 6 hai.

App 2 cheezon se chalti hai: **Docker** (`docker compose up`) aur **image service**
(`uv run imagegen/server.py`, reel ki pictures ke liye). Dono alag-alag chalane hain.

---

## Step 0 — Mac kaisa hona chahiye

| Cheez | Zaroorat |
|---|---|
| Chip | **Apple Silicon (M1 / M2 / M3 / M4)** — reel ki pictures sirf Apple chip pe banti hain |
| RAM | Kam se kam **16 GB** (24–32 GB ho to hang ka khatra nahi) |
| Disk | ~**40 GB** khali (Docker images, AI models, aur aapki videos) |
| Internet | Pehli baar setup me models download hote hain |

---

## Application kya-kya use karti hai

### Software

| Software | Kis kaam aata hai | Kahan chalta hai | Install |
|---|---|---|---|
| **Docker Desktop** | Poori app chalata hai (website, database, background workers) | Mac app | Aap install karoge (Step 1) |
| **Ollama** | Local AI (LLM) chalata hai jo reel ki script likhta hai | Mac pe seedha | Aap install karoge (Step 1) |
| **uv** | Image service ke liye Python 3.12 aur uske packages laata hai | Mac pe seedha | Aap install karoge (Step 1) |
| **Git** | Code download karne ke liye | Mac pe seedha | Aap install karoge (Step 1) |
| **Homebrew** | Mac pe Ollama jaise tools install karne ke liye | Mac pe seedha | Aap install karoge (Step 1) |
| mflux + MLX | FLUX picture model ko Mac ke GPU pe chalata hai | Mac pe seedha | Apne aap (uv khud install karta hai) |
| Python 3.11, FastAPI, Celery | App ka backend aur background jobs | Docker ke andar | Apne aap (`docker compose up --build`) |
| PostgreSQL 16 | Database (videos, clips, reels ki jaankari) | Docker ke andar | Apne aap |
| Redis 7 | Background jobs ki line (queue) | Docker ke andar | Apne aap |
| FFmpeg | Video kaatna, jodna, captions lagana, awaaz | Docker ke andar | Apne aap |
| yt-dlp + Deno | Link (YouTube etc.) se video/audio laana | Docker ke andar | Apne aap |
| Noto fonts | Hindi (Devanagari) aur English captions | Docker ke andar | Apne aap |

### AI models

| Model | Kis kaam aata hai | Size | Kahan chalta hai | Install command |
|---|---|---|---|---|
| **gemma3:4b** | Reel ki script likhta hai (hook, scenes, caption, hashtags) — tez wala, default | 3.4 GB | Ollama (Mac) | `ollama pull gemma3:4b` |
| **gemma3:12b** *(optional)* | Wahi kaam, lekin **behtar Hindi** aur purani kahaniyon (Ramayana jaisi) ki behtar jaankari — script ~2.5 guna dheere, likhte waqt ~8 GB RAM | 8.1 GB | Ollama (Mac) | `ollama pull gemma3:12b` |
| **FLUX.2-klein-4B** | Reel ke har scene ki picture banata hai | 16 GB download → 4.3 GB bachta hai | Image service (Mac GPU) | Step 4 ka command (`mflux-save`) — Hugging Face se aata hai, Ollama se nahi |
| **Kokoro v1.0** (int8) | Reel ka voiceover (Hindi, English, Spanish, French, Italian, Portuguese) | ~120 MB | Docker (`reelmaker`) | Apne aap pehli reel pe |
| **Whisper large-v3-turbo** | Video ki awaaz se text (captions, titles) — `TRANSCRIBER=local` | ~1.6 GB | Docker (`worker`) | Apne aap pehli video pe |
| OpenAI whisper-1 | Upar wala hi kaam, online aur fast — `TRANSCRIBER=openai` | — | OpenAI (internet) | Sirf `.env` me `OPENAI_API_KEY` |

> **Ollama me `gemma3:4b` zaroori hai; `gemma3:12b` optional hai** (Create a reel me
> "Script writer" se chuno). Ollama ke apne image models (jaise
> `x/flux2-klein`, `x/z-image-turbo`) download **mat** karna — Ollama ne image banana
> band kar diya hai (v0.32.6 se), isliye pictures mflux se banti hain. Picture model ka
> download command **Step 4** me hai.

---

## Step 1 — Software install karo (ek baar)

**1. Homebrew** (agar nahi hai):
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Install ke end me jo 2 lines "Next steps" me dikhti hain (PATH wali), unhe chalana.

**2. Docker Desktop:**
```bash
brew install --cask docker-desktop
```
(ya https://www.docker.com/products/docker-desktop/ se download karo.)
Docker app ek baar kholo. Settings → Resources → Memory **~8 GB** rakho.

**3. Ollama** (script likhne wala AI) — Mac on hote hi apne aap chalega:
```bash
brew install ollama
brew services start ollama
ollama pull gemma3:4b          # ~3.4 GB, script likhne ka model
ollama pull gemma3:12b         # optional, ~8.1 GB: behtar Hindi script (thoda dheere)
```

**4. uv** (image service ke liye Python yahi le aata hai):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Install ke baad terminal band karke naya kholo.

**5. Git:**
```bash
xcode-select --install
```

**Sab theek install hua? Check karo:**
```bash
docker --version
ollama --version
ollama list            # isme gemma3:4b dikhna chahiye (aur gemma3:12b, agar download kiya)
uv --version
git --version
```

---

## Step 2 — Code laao

```bash
git clone https://github.com/MohdSakib535/Clipforge.git
cd Clipforge
git checkout dev-v1
```

> **Dhyan do:** "Create a reel" wala code pehle laptop pe commit + push hona chahiye,
> tabhi naye Mac pe aayega.

---

## Step 3 — `.env` file banao

`.env` git me nahi jaati (isme secret key hoti hai). Do tareeke:

- **Purane laptop se copy karo** (sabse aasaan), ya
- Naya banao:
  ```bash
  cp .env.example .env
  ```
  Phir `.env` kholke transcription chuno:
  - `TRANSCRIBER=local` → free, Mac pe hi chalta hai (slow)
  - `TRANSCRIBER=openai` → fast, paid; tab `OPENAI_API_KEY=` me apni key daalo

---

## Step 4 — Picture model (FLUX.2-klein-4B) download karo (ek baar)

Ye model Ollama se nahi, **Hugging Face** se aata hai. Ye command use download karke
chhota (4-bit) bana ke `~/.cache/clipforge/flux2-klein-4b-q4` me rakh deta hai — wahi
jagah jahan image service use dhoondhti hai:

```bash
uvx --python 3.12 --from mflux==0.23.0 --with 'pillow<13' \
  mflux-save --model flux2-klein-4b --quantize 4 --path ~/.cache/clipforge/flux2-klein-4b-q4
```

- ~16 GB download hota hai, isliye time lagega — khatam hone tak terminal band mat karna.
- Done hone ke baad check karo (~4.3 GB dikhna chahiye):
  ```bash
  du -sh ~/.cache/clipforge/flux2-klein-4b-q4
  ```
- Ab bada 16 GB wala original download zaroori nahi — jagah bachane ke liye hata do:
  ```bash
  rm -rf ~/.cache/huggingface/hub/models--black-forest-labs--FLUX.2-klein-4B
  ```

**Download nahi karna? Purane Mac se copy karo:** wahan ye folder (4.3 GB) hai —
`~/.cache/clipforge/flux2-klein-4b-q4` — ise naye Mac pe **bilkul isi jagah** copy kar do
(pendrive / AirDrop).

Ye step chhoot gaya to bhi chalega: pehli reel ki pehli picture banate waqt image service
ye model apne aap download kar legi — bas us din pehli picture me kaafi time lagega.

---

## Step 5 — Pehli baar app chalao

**Terminal 1 — app (Docker):**
```bash
cd ~/Desktop/my_projects/Clipforge
docker compose up -d --build
```
Pehli baar images banne me kuch minute lagte hain.

**Terminal 2 — image service (reel ki pictures):**
```bash
cd ~/Desktop/my_projects/Clipforge
uv run imagegen/server.py
```
Ye terminal **khula rakho**. (Detail neeche "Image service" section me.)

Phir browser me kholo:

- **Dashboard:** http://localhost:8080
- **API docs:** http://localhost:8080/docs
- **Background jobs monitor (Flower):** http://localhost:5555

Check karne ke liye:
```bash
curl localhost:8080/health      # "status":"ok" aana chahiye
curl localhost:8189/health      # image service: "ok": true
```

---

## Step 6 — Roz ka kaam

**Start:**
```bash
docker compose up -d           # terminal 1: app
uv run imagegen/server.py      # terminal 2: image service (khula rakho)
```

**Stop:**
- Image service wale terminal me **`Ctrl + C`**
- Phir: `docker compose down`

`--build` sirf tab lagao jab code badla ho:
```bash
docker compose up -d --build
```

> "Clip a video" (purana flow) ke liye sirf Docker kaafi hai. Image service sirf
> "Create a reel" me **Generate video** ke liye chahiye.

> Aapka data (videos, clips, reels, music, database) Docker volumes me rehta hai.
> `docker compose down` se data **nahi** mitta. Sirf `docker compose down -v` se mitega —
> wo mat chalana.

---

## Reel banane ka tareeka

1. Dashboard me upar **Create a reel** pe click karo.
2. Platform (Instagram / Facebook), language, voice, length aur topic chuno → **Write script** (~20 sec).
   **Script writer** me chuno kaun likhe:
   - **Fast (gemma3 4B)** — tez (~40 sec), Hindi theek-thaak.
   - **Better Hindi (gemma3 12B)** — Hindi aur kahani behtar, ~2.5 guna time, likhte waqt ~8 GB RAM.
     Pehle `ollama pull gemma3:12b` karna hoga; download na ho to list me
     "not downloaded" dikhega. Kisi reel ka writer baad me **Script idea → Write again** se bhi badal sakte ho.
3. Script padho, chaaho to edit karo ya doosra hook chuno.
4. **Generate video** → 30–40 sec ki reel ~2–2.5 min me.
5. **Download video** aur **Copy caption + hashtags**.

Background music chahiye to pehle **Clip a video → Review → Background music** me track
upload karo; wo reel ke music list me aa jayega.

---

## Pehli baar apne aap kya download hota hai

| Kya | Size | Kab |
|---|---|---|
| Whisper (local transcription) | ~1.6 GB | Pehli video transcribe karte waqt (`TRANSCRIBER=local`) |
| Kokoro (reel ki voice) | ~120 MB | Pehli reel banate waqt |
| FLUX picture model | ~16 GB | Pehli picture pe — sirf tab, jab Step 4 chhoot gaya ho |

---

## Tips — speed aur hang se bachne ke liye

- Mac ko **charger pe** rakho — battery pe 1 min me so jata hai aur kaam ruk jata hai.
- Reel banate waqt Chrome ke bahut saare tabs / bhaari apps band rakho.
- Reel aur badi video ka clip job ek saath mat chalao — dono RAM khaate hain.
- **gemma3:12b** chuno to script likhte waqt ~8 GB RAM lagti hai (likhne ke baad turant khaali
  ho jaati hai, pictures banne se pehle). Mac slow lage to bhaari apps band karo ya
  **Fast (gemma3 4B)** chuno.

---

## Kuch galat ho to

| Problem | Kya karein |
|---|---|
| Reel me error: *"The image generator isn't running"* | Image service band hai — terminal me `uv run imagegen/server.py` chalao |
| Reel me error: *"Ollama isn't running"* | `brew services start ollama` |
| Error: *"The script model isn't installed"* | Jo writer chuna hai wo download karo: `ollama pull gemma3:4b` (ya `ollama pull gemma3:12b`) |
| `.env` badla par asar nahi hua | `docker compose up -d` chalao (`restart` purani settings rakhta hai) |
| Reel "Stopped" dikha rahi hai | Mac/Docker beech me restart hua tha — **Generate video** dobara dabao |
| Mac bahut slow / hang | Bhaari apps band karo; Docker Desktop ki memory 8 GB se zyada mat rakho |
| Port already in use | Ye ports free hone chahiye: 8080, 5433, 6380, 5555, 8189, 11434 |

---

## Image service (pictures banane wali) — terminal se start, stop aur status

Image service reel ki **pictures** banati hai. Ye Docker ke andar nahi, seedha Mac pe
chalti hai (Docker Mac ka GPU use nahi kar sakta), isliye ise terminal se khud chalana hai.

### 1. Start kaise karein

Terminal kholo aur project folder me jao (jahan code clone kiya tha):
```bash
cd ~/Desktop/my_projects/Clipforge
uv run imagegen/server.py
```
Chalu ho gayi to terminal me ye line dikhegi:
```
INFO serving on http://127.0.0.1:8189 (model /Users/<aap>/.cache/clipforge/flux2-klein-4b-q4)
```
- Ye terminal **khula rakho** — band kiya to service bhi band.
- Pehli baar chalane pe pehle "Installed ... packages" dikhega (Python packages aa rahe hain) — normal hai.

**Terminal free rakhna hai? Background me chalao:**
```bash
nohup uv run imagegen/server.py > ~/imagegen.log 2>&1 &
```
Iska output `~/imagegen.log` me jaata hai (`tail -f ~/imagegen.log` se dekh sakte ho).

### 2. Stop kaise karein

| Kaise chalayi thi | Band karne ke liye |
|---|---|
| Terminal me (upar wala `uv run`) | Usi terminal me **`Ctrl + C`** |
| Background me (`nohup`), ya terminal band ho gaya | Kisi bhi terminal me: `pkill -f imagegen/server.py` |

### 3. Pata kaise chale — chal rahi hai ya band?

```bash
curl localhost:8189/health
```

| Jo dikhe | Matlab |
|---|---|
| `{"ok": true, "loaded": false, ...}` | ✅ **Chal rahi hai** — khali baithi hai, model RAM me nahi (normal) |
| `{"ok": true, "loaded": true, ...}` | ✅ **Chal rahi hai** — model RAM me hai (picture ban rahi hai ya abhi bani) |
| `curl: (7) Failed to connect to localhost port 8189 ...` | ❌ **Band hai** |

Doosra tareeka — process dekho:
```bash
pgrep -lf imagegen/server.py
```
Kuch line dikhe = **chal rahi hai**. Kuch na dikhe = **band hai**.

**Aur nishaniyan:**
- Start karte waqt `OSError: [Errno 48] Address already in use` aaye = service **pehle se chal rahi hai** — dobara chalane ki zaroorat nahi.
- Dashboard me **Generate video** dabane pe error *"The image generator isn't running"* = service **band hai** — start karo.

### Sirf RAM khali karni ho (service band kiye bina)

```bash
curl -X POST localhost:8189/unload
```
Waise model 2 minute khali rehne ke baad apne aap RAM se hat jata hai.

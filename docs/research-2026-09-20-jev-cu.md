# Research 2026-09-20 — Jev: oficjalne źródła, krytyka skilla, plan „najszybszy Computer Use"

Cztery równoległe agenty (Sonnet 5) + własne pomiary na żywym API (30 wywołań, ~$0.004).
Każdy fakt ma URL. Rzeczy niepotwierdzone oznaczone **NIE ZNALEZIONO** albo **INFERENCJA**.

---

## 1. Co mówią oficjalne źródła (i czego skill nie wie)

### 1.1 Indeks docs — `https://docs.typesafe.ai/llms.txt`
Pełna mapa dokumentacji w jednym pliku (każda strona ma wersję `.md`). Skill linkuje tylko
`docs.typesafe.ai` — powinien linkować `llms.txt`, bo to jedyny sposób, by agent trafił do
cookbooków i strony o słabościach modelu.

### 1.2 Strona „model jaggedness" — **skill jej nie zna, a to najważniejsza strona dla promptowania**
`https://docs.typesafe.ai/model-jaggedness/jev-1.13.md` (last reviewed 2026-09-17), 11 udokumentowanych trybów porażki:

| Tryb | Cytat producenta | Konsekwencja dla skilla / CU |
|---|---|---|
| Literal reading | „answers the question you wrote, not the one you meant" | warunki brzegowe wprost w `criteria` |
| Math / counting | „Jev is not a calculator", „does not count reliably" | liczenie w kodzie, nigdy w pytaniu |
| Numeric representations | hex, „near each other" — słabo | konwertuj i bucketuj w kodzie |
| Math using Score | poziomy Score „weak in numerical calibration" | Score tylko do progów, nie do interpolacji |
| Date/time | „reads dates as text, not as ordered quantities" | daty parsuj w kodzie (tak robi typesafe-computer-use) |
| Indirection | podwójne negacje, pośrednie odwołania — gorzej | pytania proste, nazwany cel |
| Large irrelevant state | „Unrelated detail acts as a distractor" | REDUCE w kodzie przed wysłaniem |
| Adversarial content | „does not treat [data] as hostile by default" | **UI może zawierać prompt-injection; guard w kodzie** |
| Contradictory instructions/criteria | model „might get confused" | lint spójności bundle'a pytań |
| Structural invariants | **P(x) ≠ 1 − P(¬x)** nie jest gwarantowane | nie licz na tożsamości między pytaniami |
| Generation | „not trained to generate text", „very slow" jeśli wymusić | tekst do pól pisze mały LLM |

### 1.3 Oficjalne wzorce (`/patterns.md`) — skill ma własną 9-elementową taksonomię bez mapowania
- **Speculative Fan-Out** — „Send many questions in a single call, including speculative ones, and let your code decide what's relevant." `/patterns/fan-out.md`
- **Confidence-Gated Routing** — „The answer tells you what; confidence tells you whether to act." Przykład progów: floor 0.6 → człowiek; 0.6–0.85 na akcji wysokiego ryzyka → potwierdzenie; >0.85 → auto. **„Each action type has its own threshold based on the consequences."** `/patterns/confidence-routing.md`
- **Composite Scoring**, **Intent Routing** — `/patterns/composite-scoring.md`, `/patterns/intent-routing.md`

### 1.4 Cookbooki z liczbami (skill nie cytuje żadnego)
- **parallel_questions** — 13 pytań w 1 wywołaniu: **0.27 s vs 2.71 s** sekwencyjnie; **$0.000497 vs $0.006090**; „12.2x cheaper, 10.0x faster" (jev-1.12, 5 runów). To *oficjalne* źródło dla naszego „12.4×" — skill podaje raz 9.4×, raz 12.4× (patrz §2).
- **skill_suggestion** — 182 opcje jako *criteria* jednego Choice + 3 Nouly bramkujące (średnia < 0.30 → nic), potem re-rank top-3 z pełnym opisem + Noul „fits" per kandydat (> 0.30). 488 requestów: złe załadowania 16.8% → 7.3%, zbędne 9.8% → 4.0%. **Dokładny kształt kaskady dla wyboru elementu UI.**
- **hierarchical_classification** — beam search (K=3, geometric mean ścieżki) 4/4 vs greedy 2/4.
- **rerank_typesafe** — BM25 top-30 + Noul per kandydat: top-1 5% → 18%, top-10 38% → 62%; 1 200 wywołań = $0.0645.
- **consistency_choice_cookbook** — powtarzalność 90.8% surowa; z progiem 0.60 → 99.2% (74.2% auto-routowane). „For application decisions, we also require a top probability of at least 0.60."
- **function_calling** — argumenty zamknięte → Choice/Set/Flag, pytanie „stated" czy użytkownik w ogóle wspomniał; confidence = **najsłabsze** pytanie, nie iloczyn. „Write each question about the idea rather than the words a user might pick."
- **sde_cascade** — mini-model ekstrahuje → Jev weryfikuje pola → tylko flagi idą do drogiego modelu.

### 1.5 API — fakty potwierdzone i niepotwierdzone
- Endpoint vendora `POST https://api.typesafe.ai/v1/systemone`, `GET /v1/models`; typy pytań **dokładnie** noul/choice/score; Choice ≤ 255 opcji; Score 2–10 poziomów; błędy 401/422/429/529. `/api.md`
- Limity rate **potwierdzone** na `/models.md` (sprawdzone osobiście): „250,000 tokens per second / 1,200 requests per minute", „Rate limits are adjusting dynamically"; „64k tokens per request; 32k tokens for `state` plus the longest question". Agent A ich nie znalazł, agent D i mój fetch — tak.
- **`session_id`, `user`, `provider`, `trace` NIE są polami schematu vendora** (agent D: sygnatura oficjalnego SDK `system_one(state, questions, model, retry, timeout, extra_headers, extra_body, response_model)`). To generyczne opcje OpenRouter. Skill (`api.md:675-676`) prezentuje je jako część request body.
- Limit bajtów, max liczba pytań: NIE ZNALEZIONO (ogranicza tylko budżet tokenów).
- Dodatkowe (agent D): Cloudflare Workers AI serwuje model jako `typesafe/jev`; Vercel AI Gateway ma **darmowe okno do 2026-09-25** (karta wymagana) i eksperymentalne `evaluate` API w AI SDK 7; `jev-preview` = ten sam build co `jev-latest` („no preview build available right now"); ścieżki backtick obsługują indeksy tablic (`support.tickets[0].message`); vendor mówi, że cena jest subsydiowana i ma spadać.
- Cena $0.042/Mtok input, output darmowy — potwierdzone na typesafe.ai i OpenRouter. Free tier: NIE ZNALEZIONO.
- Wersje: `jev-1.13.0`; `jev-latest` = `jev-1.13.0`; `jev-preview` obecnie identyczny. `/models.md`
- OpenRouter ma też alias **`~typesafe/jev-latest`** (dodany 2026-09-18) — skill go nie zna.
- Latencja oficjalna: **70–500 ms end-to-end**, „adding questions barely changes the response time". Brak oficjalnego p50/p95, brak krzywej latencja-vs-rozmiar stanu.
- Oficjalne SDK: `pip install typesafe_sdk` (`TypeSafeClient`, `AsyncTypeSafeClient`, `.system_one(state, questions)`), `npm i @typesafe-ai/sdk` (helpery `choice()`, `noul()`, `score()`). Skill reimplementuje klienta i nie wspomina o SDK — dla instalacji zero-dep to OK, ale skill powinien powiedzieć *dlaczego*.
- Oficjalny skill agenta: `claude plugin marketplace add typesafe-ai/skills` + `claude plugin install typesafe@typesafe-ai` (nasz `api.md` podaje tylko `npx skills add`). Uczy 6 wzorców, w tym **„state-responsive decisions: retain goals while fresh judgments guide bounded next steps"** — najbliższe oficjalne sformułowanie pętli agenta GUI. Rada producenta: „Put the constants (questions and thresholds) in a single place so they're easy to review."

### 1.6 „Niezależne badanie 2 000 e-maili" — ZNALEZIONE
`https://github.com/anisselbd/jev-phishing-bench` (2026-09-17, PhishNChips v5.2): Jev solo **62.6%** (AUROC 0.689, ECE 0.154) vs Haiku 4.5 **81.3%**; Jev 27× tańszy ($0.038 vs $0.462 / 1 000), p50 239 ms vs 687 ms. Pięć sygnałów + regresja logistyczna: **95.1%** [93.5–96.2], AUROC 0.988, ECE 0.027 — statystycznie remis z Haiku na tych samych sygnałach (93.2%). **Regex baseline: 91.8%.** Skill cytuje to 3× bez URL i bez liczby 62.6%.

Agregator niezależnych benchmarków: `https://jevbench.xyz/methodology` — dokładność Jev per zadanie 62.6–95.4%; „no single Jev accuracy number exists".

---

## 2. Krytyka obecnego skilla — jak jest napisany

Ocena wobec: spec Agent Skills (SKILL.md = zwięzły przewodnik decyzyjny, progressive disclosure), własnej reguły z `AGENTS.md` („decision guide for an agent, not documentation for a human", „every published number reproducible") i oficjalnych docs.

### 2.1 Błędy faktyczne / sprzeczności między plikami (łamią AGENTS.md)
| # | Gdzie | Problem |
|---|---|---|
| F1 | `SKILL.md:245`, `patterns.md:521` **9.4×** vs `README.md:130,501`, `AGENTS.md:102`, `benchmarks.md:152` **12.4×** | ta sama liczba, dwie wartości |
| F2 | `SKILL.md:281` „~330 → ~5 000 tokenów przesunęło p50 o **<10 ms**" vs `README.md:502` „324 → 7 020: **353 → 468 ms**" | 115 ms ≠ „<10 ms" |
| F3 | `SKILL.md:273`, `patterns.md:370`, `prompting.md:113` „independent 2,000-email study" | brak URL, brak liczby 62.6%, brak informacji, że regex daje 91.8% |
| F4 | `api.md:675-676` pola `session_id`, `user`, `provider`, `trace` w „Request" | to opcje OpenRouter, nie schemat vendora (limity rate z `api.md:545` są OK — potwierdzone na `/models.md`) |
| F10 | `client.py:301-303` doklejał `session_id` do body **niezależnie od providera** | **POTWIERDZONE na żywo 2026-09-20**: vendor zwraca `400 {"detail":{"error_type":"api_usage_error","message":"Invalid request."}}`. Naprawione w 0.11.0 (`accepts_session_id` w `PROVIDERS`) |
| F11 | `config.py` `Config.from_env` wybierał klucz **przed** rozstrzygnięciem providera | **POTWIERDZONE na żywo**: `JEVSKILL_PROVIDER=typesafe` + klucz OpenRouter w env → klucz OpenRouter poszedł do vendora → `401`. Naprawione w 0.11.0 (`resolve_provider_intent` najpierw, potem klucz w zakresie providera; bez intencji nazwy vendora przed agregatorem, per nazwa env→rejestr) |
| F12 | `cli.py` doctor `key_source_hint` = pierwsze 12 znaków klucza | częściowy sekret w każdym logu z `doctor`; zastąpione `key_name`/`key_source`/`key_fingerprint` (SHA-256[:8]) |
| F5 | `api.md` „Verified against the live APIs", `AGENTS.md` „Both existing providers were written against live calls" vs `CHANGELOG` Unreleased: vendor „not by a real call — no TypeSafe key was available" | endpoint vendora nigdy nie był wywołany, a skill mówi, że tak |
| F6 | `SKILL.md:34` „~325 ms (p50)" jako stała cecha modelu | to pomiar z Polski jednego dnia; dziś `doctor` dał 651 ms, bench 325–371 ms; vendor mówi 70–500 ms zależnie od lokalizacji |
| F7 | `SKILL.md:18` `allowed-tools: Bash(python:*)` | każdy przykład w §0–§7 to `jevskill …`, nie `python …`; na Linux/macOS `python3` |
| F8 | `SKILL.md:263-270, 315-324` snippety `jev.decide`, `noul(...)`, `next_round`, `combine_weighted` | `jevskill/__init__.py` eksportuje tylko `__version__`; agent nie ma jak tego zaimportować |
| F9 | `SKILL.md:60`, `README` `python scripts/jev_query.py` | ścieżka względna do katalogu skilla; cwd agenta to projekt. W Claude Code katalog bazowy jest podany przy załadowaniu; w innych agentach nie — trzeba napisać jak go znaleźć |

### 2.2 Braki wobec oficjalnych docs
- Zero wzmianki o **model-jaggedness** (11 trybów porażki) — a to bezpośrednio uzasadnia reguły z `prompting.md` i dodaje nowe (daty, liczenie, adversarial UI, P(x)≠1−P(¬x)).
- Zero mapowania własnych 9 wzorców na 4 oficjalne + cookbooki. Agent, który zna oficjalny skill TypeSafe, nie znajdzie wspólnego języka.
- Brak `llms.txt`, brak cookbooków z liczbami, brak SDK, brak aliasu `~typesafe/jev-latest`, brak instalacji przez `claude plugin`.
- Brak reguły **„porównania dwóch stanów robi kod"** — pomiar dziś: pytanie `stuck` (czy ekran się zmienił po akcji) daje 0.42–0.56 przy stanie, który *ewidentnie* się zmienił. Hash/diff stanu w kodzie: 0 ms, deterministyczny.

### 2.3 Struktura i styl (to, co „tańszy model" zrobił źle)
- **499/500 linii, ~40% to proza pomiarowa dla człowieka**: §6 (ledger, `advice`, tabela stage'ów, „how you know the breakdown is not inflated") ≈ 70 linii; §0 tabela dwóch endpointów ≈ 30 linii; wstęp z „Measured, not estimated". Agent podejmujący decyzję potrzebuje: kiedy Jev / kiedy nie, jak zbudować pytanie, jak wywołać, jak czytać wynik, jak eskalować. Reszta → `references/`.
- **Dwa konkurencyjne punkty wejścia** (`scripts/jev_query.py` zero-install vs `jevskill` CLI) bez jednoznacznej reguły „użyj X, chyba że Y". Agent traci turę na wybór.
- **Description** we frontmatter ≈ 1 000 znaków (limit ~1 024): zawiera politykę („Advisory, never an authorization boundary…") — polityka do ciała, description ma odpowiadać na „kiedy mnie załadować".
- Sekcja „No key? Ask" — dobra polityka, 20 linii; da się w 6.
- Powtórzenia: exit codes w `SKILL.md`, `README`, `commands.md`, `jev_query.py`; reguła „name the value" w 4 plikach; „2,000-email" w 3.
- Niespójny przykład: `commands.md` `Does \`state\` break…` vs `SKILL.md` „Does the diff change…" — kiedy nazywać `state` backtickiem, gdy stan jest stringiem? Vendor: ścieżki backtick są dla wartości zagnieżdżonych.
- Tabela `stats.ADVICE` z „STOP/ESCALATE" i „the `STOP` verdict is the one to look for first" — to instrukcja dla utrzymującego repo, nie dla agenta w trakcie zadania.
- `patterns.md` ma **dwie** tabele anty-wzorców z częściowo tymi samymi wierszami (`Looping one question per call` ×2, `giant question` ×2).

### 2.4 Klient a pętla agenta
`client.py` docstring: „a short burst of decisions, **not a hot loop**". Dla CU trzeba: timeout ~1.5 s zamiast 60 s, hedged request (duplikat po ~400 ms, bierz pierwszy — koszt 2× tylko na ogonie), async, ledger zapisywany poza gorącą ścieżką, prekompilowane bajty pytań (jest), HTTP/2 keep-alive (jest), brak regexów redakcji na każdym kroku (albo zmierzony koszt).

---

## 3. Pomiar własny (2026-09-20, Polska → OpenRouter, httpx/h2)
Stan = drzewo UI N elementów (`role`,`name`,`bbox`,`enabled`,`focused`) + `goal` + `last_action`; 4 pytania w jednym wywołaniu: `target` (Choice N+`none`), `action` (Choice 6), `goal_reached` (Noul), `stuck` (Noul). K=10 warm.

| N elementów | tokens_in | koszt/wyw. | http p50 | min | max | target | conf |
|---:|---:|---:|---:|---:|---:|---|---:|
| 12 | 1 723 | $0.000072 | 367 ms | 291 | 485 | e11 „Save" 0.99 | 0.99 |
| 30 | 3 308 | $0.000139 | 325 ms | 295 | 431 | e11 0.99 | 0.98 |
| 60 | 6 041 | $0.000254 | 371 ms | 322 | 442 | e11 0.99 | 0.98 |

Wnioski: latencja płaska wobec N (potwierdza vendora), wybór celu stabilny do 60 kandydatów, `action=click` 0.69–0.82 (spada z N), `goal_reached` 0.04 (poprawnie), **`stuck` 0.42–0.56 = pytanie źle postawione** (porównanie stanów → kod). Brak zawieszeń w 30 wywołaniach (twierdzenie „~15% timeoutów na OpenRouter" ze źródeł trzecich — nie potwierdzone tutaj).

**Ten sam bench na końcówce vendora** (`JEVSKILL_PROVIDER=typesafe`, te same minuty, klucz `JEV_API_KEY`):

| N | tokens_in | vendor http p50 | min | max | OpenRouter p50 (wyżej) | różnica |
|---:|---:|---:|---:|---:|---:|---:|
| 12 | 1 723 | **304 ms** | 281 | 333 | 367 ms | −63 ms |
| 30 | 3 308 | **292 ms** | 264 | 355 | 325 ms | −33 ms |
| 60 | 6 041 | **320 ms** | 288 | 401 | 371 ms | −51 ms |

Vendor o hop mniej: 30–60 ms szybciej i węższy ogon (max 333–401 vs 431–485). `stuck` na vendorze 0.47–0.60 — to samo, więc to pytanie, nie provider. `warm` (HEAD `/v1/models`) na vendorze ~600 ms vs ~90 ms na OpenRouter — do zbadania (brak keep-alive po HEAD? wolniejsza lista modeli?); dla pętli CU rozgrzewać prawdziwym mini-wywołaniem, nie HEAD.
Skrypt: `bench/cu_bench.py` (`python bench/cu_bench.py 10`; provider przez `JEVSKILL_PROVIDER`).

---

## 4. Konkurencja: kto już zrobił Computer Use na Jev

| Projekt | Platforma | Stan | Decyzja/krok | Liczby |
|---|---|---|---|---|
| **browser-use/jev-ultrafast** (2.5k★) | Chrome via Browser Harness (CDP) | tabela elementów `[1] button …`, tylko widoczny tekst | op Choice (CLICK/TYPE_TEXT/SELECT/SCROLL_UP/DOWN/WAIT/DONE/BLOCKED) + target Choice **w jednym round-tripie**; mały LLM (`inception/mercury-2.5`) tylko dla TYPE_TEXT | Google Flights **7.07 s** (z 9.45 s), CDP calls 1 092 → 101; Wikipedia 2.8 s; hotel 1.9 s; $0.0039/task. Settle: combobox ≤200 ms, inne ≤2 klatki/50 ms. „A `DONE` choice still requires independent outcome verification." |
| **awlevin/typesafe-computer-use** | macOS (Quartz, AX, Vision OCR) | OCR (tile-diff 256 px, re-OCR tylko zmienionych kafli) + AX tree → jedna numerowana lista `ocr/ax/ax+ocr` | 3 Choice: kind (11 opcji), item, site/url + gate conf ≥ 0.4; Noul „sensible value" po wpisaniu | **krok: capture 0.31 + ocr 0.31 + ax 0.06 + decide 0.21 + act 0.05 = 0.95 s**; model 0.13–0.38 s vs Opus 5.2 s; $0.0002/decyzja; AX coverage: Finder 100%, Chrome 88%, Slack 85%, Spotify 0% |
| **kofanlabs/typesafe-computer-use-windows** | Windows (PrintWindow, Windows.Media.Ocr, UIA) | j.w. | j.w. + MCP host handoff | grid form 5.5 s; „2.243 s UI interaction after host reply"; PrintWindow pada na GPU-rendered |
| jkudish/jev-browser | Playwright | tekst strony + DOM ≤ 240 el. | action Choice + goal Noul + stuck Noul | Wikipedia Coffee→Espresso ~4 s, $0.0016; stop: goal > 0.85 / stuck > 0.85 |
| Inne | — | — | — | droidrun/mobile-jev (Android, ~21 s / 9 akcji), chy4pro/jev-for-chrome (17 zadań), Yappy (275–690 ms/decyzja), ProgressGate (stagnacja pętli) |

Lista zbiorcza: `https://github.com/AnotiaWang/awesome-jev`. Niezależny hub: `https://systemonemodels.org`.

**Wniosek**: nikt nie zrobił jeszcze pętli z (a) UIA-first bez OCR na gorącej ścieżce, (b) diffem stanu w kodzie, (c) hedged requests, (d) cache makr i (e) spekulacyjnym planem wielokrokowym. To jest przestrzeń na „najszybszy".

---

## 5. Gdzie ucieka czas w agentach GUI (literatura, agent C)
- **OSWorld-Human** (arXiv 2506.16042): planowanie 53–75% czasu kroku, refleksja do 34%, grounding 1.8–3.9%, screenshot+akcja ~1–3%; agenci robią **2.7–4.3× więcej kroków niż człowiek**; koszt $2.43/task, 87% to planowanie.
- **„Why Are GUI Agents Correct but Late?"** (arXiv 2607.28399): reaktywna pętla 567 ms p50; **prekompilowane drzewa decyzji w idle** → na gorącej ścieżce tylko lekki obserwator ~325 ms.
- **Prefill dominuje** grounding VLM (arXiv 2605.12549) — mniej tokenów obrazu > szybszy dekoder. Jev omija to całkowicie (tekst).
- Speculative Macro Commit (arXiv 2609.03236), AgenticCache (2604.24039), Speculative Actions (2510.04371, 1.5–2×): spekulacja + makra to znany kierunek, nie zwalidowany na Jev.
- Windows: DXGI Desktop Duplication potrafi zawiesić `AcquireNextFrame` > 1 s; PrintWindow pada na GPU; **UIA nie ma publicznego benchmarku ms** (INFERENCJA 10–100 ms dla okna).
- Ryzyka dla „najszybszy": (1) luki w UIA (Electron, canvas, Spotify 0%) wymuszają OCR/VLM ≥ 600 ms; (2) grounding nigdy nie był wąskim gardłem — **liczba kroków** jest; (3) stały zbiór opcji nie ma odpowiedzi na nieznane dialogi/CAPTCHA — detekcja „kiedy eskalować" jest nierozwiązana.

---

## 6. Plan: „najszybszy Computer Use na Jev" (Windows)

### 6.1 Budżet kroku (cel)
| Etap | Cel | Jak |
|---|---|---|
| Stan | 10–60 ms | UIA tylko aktywne okno, cache drzewa + `StructureChanged` events; **bez OCR na gorącej ścieżce** (OCR async tylko dla regionów bez UIA, wynik dołącza do *następnego* kroku) |
| Redukcja | ≤ 2 ms | kod: widoczne ∧ enabled ∧ rola interaktywna; dedupe nazw; ≤ 60 kandydatów (pomiar: 0.99 do N=60); powyżej → kaskada region→element (skill_suggestion) |
| Decyzja | ~300 ms (floor sieci) | 1 wywołanie, speculative fan-out: `target`, `op`, `goal_reached`, `needs_text`, `is_destructive`; **hedged request** po 400 ms; timeout 1.5 s |
| Walidacja | < 1 ms | kod: element nadal istnieje, bbox na ekranie, nie zasłonięty; `op` ∈ dozwolone dla roli; `is_destructive` ∧ conf < próg → confirm |
| Akcja | ~5 ms | UIA `Invoke`/`SetValue` gdy dostępne, inaczej `SendInput` |
| Settle | 0–200 ms | czekaj na UIA event lub zmianę hasha drzewa; cap 200 ms (jev-ultrafast: 50 ms / 2 klatki) |
| **Razem** | **~350–500 ms** | vs 0.95 s (typesafe-computer-use), 567 ms (paper) |

### 6.2 Poza gorącą ścieżką (to daje przewagę, nie mikro-optymalizacje)
1. **Diff stanu w kodzie** — `stuck`, „ekran się zmienił", „dialog pojawił się" = hash drzewa przed/po. Dzisiejszy pomiar pokazuje, że Jev tego nie rozstrzyga (0.5).
2. **Cache makr**: klucz = (goal_norm, hash znormalizowanego drzewa) → decyzja; powtórzone zadanie = 0 ms i $0. Ledger `jevskill` już ma cache byte-identical — potrzebna normalizacja stanu (bez bbox, bez timestampów).
3. **Spekulacyjny plan**: przy starcie zadania jedno wywołanie LLM (lub Jev nad katalogiem makr) daje 3–5 kroków oczekiwanych; każdy krok Jev tylko *weryfikuje* („czy `plan[0]` nadal pasuje?") — mniej kroków = mniej czasu (OSWorld-Human: kroki, nie grounding).
4. **Tekst do pól**: mały LLM wywołany **równolegle** z Jev, gdy `needs_text` prawdopodobne z celu (pre-generacja na starcie zadania), Noul „sensible value" po wpisaniu (typesafe-computer-use).
5. **Bezpieczeństwo**: confidence-gated routing per typ akcji (vendor: floor 0.6, 0.85 dla nieodwracalnych), guard `is_destructive` + deterministyczna lista (Delete, Send, Pay, Format) — Jev doradza, kod decyduje; UI może zawierać injection (jaggedness).
6. **Eskalacja**: `target=none` ∨ conf < floor ∨ 2× brak zmiany hasha → VLM/człowiek. Mierzyć, jak często — to decyduje o średniej.

### 6.3 Pomiar, którym trzeba to obronić
`bench/cu_bench.py`: (a) latencja vs N i vs lokalizacja (OpenRouter vs `api.typesafe.ai` — **potrzebny klucz vendora**, nigdy nie zmierzony); (b) hedged vs plain p95; (c) 10 zadań Windows (Notatnik, Eksplorator, Ustawienia, Chrome) × 3 runy: czas, kroki, koszt, sukces; (d) porównanie z `kofanlabs/typesafe-computer-use-windows` na tych samych zadaniach.

---

## 7. Lista napraw skilla (priorytet)
1. **P0 fakty**: F1–F10 z §2.1; jeden test `test_published_metadata` sprawdzający, że każda liczba w `SKILL.md` występuje w `bench/*.json`. Werdykt agenta D: **rdzeń skilla (endpointy, id modelu, cena, kontekst, limity opcji, kształty odpowiedzi, brak `cost` u vendora, liczby z phishing-bench) zgadza się ze źródłami** — błędy są w otoczce, nie w kontrakcie API.
2. **P0 treść**: sekcja „Znane słabości modelu" z 11 trybów (link do jaggedness) → reguły w `prompting.md`; reguła „porównania i liczenie robi kod".
3. **P0 struktura**: `SKILL.md` ≤ 250 linii: kiedy/kiedy-nie, 3 prymitywy, jedna ścieżka wywołania (zero-install domyślnie, CLI gdy zainstalowany), 4 reguły, eskalacja, router do `references/`. §6 (ledger/advice/stage) → `references/measure.md`.
4. **P1 mapowanie**: tabela nasze 9 ↔ oficjalne 4 + cookbooki z URL; `llms.txt`; SDK; `~typesafe/jev-latest`; `claude plugin install`.
5. **P1 poprawność**: `allowed-tools` zgodne z komendami; usunąć snippety Python albo wyeksportować API; ścieżka do skryptów (`$SKILL_DIR`).
6. **P2 CU**: nowa rodzina wzorca **act** (`references/act.md`): stan UI → kandydaci → fan-out → walidacja → akcja → diff; klient hot-loop (`JevClient(hot=True)`: timeout, hedging, async ledger).
7. **P2 źródła**: URL do phishing-bench + 62.6%/91.8%; wpis do CHANGELOG o wycofanych liczbach (9.4×, „<10 ms").

---

## Źródła (główne)
- docs: https://docs.typesafe.ai/llms.txt · /api.md · /models.md · /patterns.md · /patterns/fan-out.md · /patterns/confidence-routing.md · /model-jaggedness/jev-1.13.md · /concepts/how-to-build-with-system-one.md · /agent-skill.md
- cookbooki: /cookbooks/parallel_questions.md · /cookbooks/skill_suggestion.md · /cookbooks/function_calling.md · /cookbooks/hierarchical_classification.md · /cookbooks/rerank_typesafe.md · /cookbooks/consistency_choice_cookbook.md · /cookbooks/sde_cascade.md
- vendor: https://typesafe.ai · https://typesafe.ai/blog/introducing-system-one-models-and-jev · https://github.com/typesafe-ai/skills · https://openrouter.ai/typesafe
- CU: https://github.com/browser-use/jev-ultrafast · https://github.com/awlevin/typesafe-computer-use · https://github.com/kofanlabs/typesafe-computer-use-windows · https://github.com/jkudish/jev-browser · https://github.com/AnotiaWang/awesome-jev · https://systemonemodels.org
- benchmarki: https://github.com/anisselbd/jev-phishing-bench · https://jevbench.xyz/methodology
- literatura: arXiv 2506.16042 (OSWorld-Human) · 2607.28399 (Correct but Late) · 2605.12549 (prefill) · 2609.03236 (SMC) · 2604.24039 (AgenticCache) · 2510.04371 (Speculative Actions) · https://www.theregister.com/ai-and-ml/2026/09/16/typesafe-ai-debuts-model-for-machines-that-plays-doom/5296711

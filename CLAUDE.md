# aika_ingestion_pubmed — memoria di progetto

Connettore PubMed/PMC: **primo componente dell'ingestion** della pipeline RAG sull'alimentazione del
paziente in dialisi (ingestion → chunker → indexing su Qdrant con embedding BGE-M3 via servizio gRPC
esistente → agent di interrogazione). L'utente finale è il paziente: il corpus deve contenere **solo
fonti affidabili e approvate da un clinico**. Il connettore cerca su PubMed, scarica solo il full text
dell'**Open Access Subset di PMC**, converte JATS → Markdown + front matter YAML (il "contratto" verso
il chunker) e apre una **PR** su un repository corpus separato (GitHub App a permessi minimi). Scrive
sempre `curation_status: pending`; l'approvazione è del revisore clinico, **in PR**, prima del merge.

## Stato (2026-09-27)

v1 completa e testata (179 test verdi). **In uso contro NCBI e GitHub reali** dal 2026-09-26:

- Repo corpus: `indyos/aika_doc_ingestion` (privato, piano gratuito), clone in `../aika_doc_ingestion`.
  GitHub App `aika-doc-ingestion-auth` (App ID in `.env`, permessi Contents + Pull requests, read & write).
- Due run reali: 200/271 risultati esaminati → 62 documenti in PR #1–#4; dopo aver attivato il filtro
  `pubmed pmc[sb]`, altri 22 in PR #5–#6. Esclusi: 104 senza PMCID, 28 licenza non ammessa, 4 senza full
  text OA, 2 licenza mancante.
- CI e flusso di revisione installati sul repo corpus (PR #7–#11, tutte mergiate): vedi
  [Flusso di revisione](#flusso-di-revisione-del-corpus) sotto.
- Revisione in corso: PR **#4 e #6 mergiate** (4 documenti `approved` su `main`, 0 `rejected`). PR
  **#1, #2, #3, #5 ancora aperte** con documenti `pending` da rivedere.
- `.git` locale di questa cartella: inizializzato, remote `origin` → `aika_ingestion_pubmed` su GitHub,
  un commit (`init`). Le modifiche di questa sessione (comando `review`, `corpus_ci/`, filtro OA, query,
  `.env`/`python-dotenv`) sono nel working tree; verificare `git status`/`git log` prima di assumere cosa
  è già committato.

## Comandi

```bash
uv sync                                   # dopo la copia in questa cartella .venv va ricreato
uv run pytest                             # ~70-130 s (i test git su Windows sono lenti); --block-network attivo
uv run ruff check && uv run ruff format --check && uv run pyright
uv run aika-ingestion-pubmed schema       # rigenera schema/front_matter.schema.json (poi committarlo)
uv run aika-ingestion-pubmed run --dry-run   # cerca+converte, non scrive nulla (usa la rete NCBI)
uv run aika-ingestion-pubmed run
uv run aika-ingestion-pubmed review approve|reject PMID… --branch ingest/… [--notes …] [--all-pending]
uv run pre-commit install                 # hook locali: ruff, format, pyright, schema-in-sync
```

Ambiente: VS Code + uv. `.vscode/settings.json` (interprete `.venv`, pytest, Ruff al salvataggio, LF/UTF-8,
`PYTHONUTF8=1`) e `.vscode/extensions.json` sono già nel progetto: aprire **questa** cartella, non `healthcare-AI`.
`.gitignore` ignora `.venv`, `.env`, `*.pem`, `/.aika/`, `/originals/`: segreti e chiavi non finiscono nei commit.

`config.yaml` (già configurato per l'uso reale): `ncbi.email: indyos71@gmail.com`,
`corpus.repo: indyos/aika_doc_ingestion`, `corpus.local_path: ../aika_doc_ingestion`. Segreti solo da
ambiente, in locale anche da **`.env`** nella cwd (gitignorato; caricato da `cli._run`/`cli._review` con
`python-dotenv`, senza sovrascrivere variabili già presenti nell'ambiente): `NCBI_API_KEY`,
`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` o `GITHUB_APP_PRIVATE_KEY_PATH`
(oggi punta a `.aika/aika-doc-ingestion-auth.….private-key.pem` dentro il progetto, gitignorato da `/.aika/`).

## Architettura (src/aika_ingestion_pubmed/)

`eutils.py` EutilsClient · `search.py` PubMedSearch · `pubmed_parser.py` · `pmc.py` PmcFullTextFetcher
· `jats.py` JatsToMarkdown (+ `inline.py` rendering inline condiviso) · `builder.py` FrontMatterBuilder
(+ `licenses.py`) · `quality.py` QualityGate · `document.py` (serializzazione, hash, parse) ·
`corpus.py` CorpusWriter (git via subprocess; incluse le primitive per la revisione: `open_branch`,
`branch_pmids`, `read_file`, `commit_changes`) · `review.py` (approve/reject nel branch di una PR) ·
`github.py` GitHubAppHost · `pipeline.py` (flusso) · `cli.py`, `config.py`, `models.py` (Pydantic →
JSON Schema), `schema.py`, `xmlutil.py`, `logging_setup.py`.

`corpus_ci/` (root del progetto, **non** in `src/`): sorgenti degli script e dei workflow GitHub Actions
da installare nel repository corpus (vedi sezione successiva). Non fa parte del pacchetto Python; ha
copie testate in `tests/test_review_script.py` e `tests/test_review_comment.py`.

## Invarianti da non rompere

- **Solo E-utilities** `esearch.fcgi`/`efetch.fcgi` (`db=pubmed|pmc`). Mai `oa.fcgi` né FTP PMC (dismessi
  ad agosto 2026). `EutilsClient` rifiuta ogni altro endpoint.
- **Solo POST**: `api_key` resta nel corpo, mai in URL/log/messaggi d'errore. `tool` ed `email` sempre presenti.
  Rate limit 3/s (10/s con key), retry con backoff su 429/5xx/errori di rete.
- **Determinismo byte per byte**: stesso XML → stesso file. Front matter con ordine chiavi fisso (= ordine dei
  campi di `FrontMatter`), opzionali vuote omesse, PMID sempre stringa, LF, UTF-8, NFC.
- `content_hash` = sha256 del **solo corpo** (così `retrieved_at` non genera diff, e la revisione — che
  cambia solo `curation_status`/`review_notes` — non lo invalida mai).
- Nessun articolo senza `<body>` non vuoto nel corpus. Nessun segreto nel repository.
- Se cambia `models.py`: rigenerare lo schema (un test e l'hook pre-commit lo verificano) **e** ricopiare
  `schema/front_matter.schema.json` nel repo corpus.
- Pyright è **strict su `src/`**; il tipo privato di lxml si nomina solo in `xmlutil.Element`.
- **Su `main` del repo corpus solo documenti già revisionati** (vedi sotto): il connettore non lo richiede
  a livello di codice, è la CI del repo corpus (`corpus_ci/validate_corpus.py`) a farlo rispettare.

## Flusso di revisione del corpus

Deciso il 2026-09-26: **la PR è il momento della revisione**; `main` del repo corpus contiene solo
documenti già decisi dal clinico, mai `pending`.

- Il connettore apre PR con documenti `curation_status: pending` in `corpus/pmid-<PMID>.md`.
- Il revisore, **dentro la PR**, decide file per file:
  - **approva** → resta in `corpus/`, `curation_status: approved`;
  - **rifiuta** → si sposta in `rejected/pmid-<PMID>.md`, `curation_status: rejected` + `review_notes`
    (motivazione obbligatoria); i file in `rejected/` non sono mai letti dal chunker e il connettore non
    riscarica più quel PMID (lo riconosce leggendo `rejected/` su `main` o nelle PR aperte);
  - **dubbio** (`suspended`) → si lascia il file fuori dalla PR (non gestito automaticamente).
- Il corpo del documento (e quindi `content_hash`) non cambia mai in fase di revisione.
- La CI del repo corpus (`validate-corpus.yml` + `scripts/validate_corpus.py`) è **rossa** finché in
  `corpus/` resta un file `pending`, o in `rejected/` un file senza `review_notes`, o un PMID è duplicato
  fra le due cartelle. Si mergia solo a verde. **Nota:** repo privato su account gratuito → le branch
  protection non sono applicate (GitHub lo dice esplicitamente), quindi il verde è una regola di processo,
  non un blocco tecnico; chiunque abbia scrittura può comunque mergiare col rosso.

Tre modi equivalenti per applicare le decisioni (stessa logica, stesso output byte-per-byte, tutti testati):

1. **CLI** (in locale, con `.env` configurato):
   `uv run aika-ingestion-pubmed review approve|reject PMID… --branch ingest/… [--notes …] [--all-pending]`.
2. **Workflow «Review documenti»** (`corpus_ci/review.py` + `review.yml`, installati come
   `scripts/review.py` + `.github/workflows/review.yml` nel repo corpus): Actions → *Review documenti* →
   *Run workflow* → nel menu **«Use workflow from»** si sceglie il branch `ingest/…` (non è un campo di
   testo), poi `decision`/`pmids` (anche `all-pending`)/`notes`. Adatto a revisionare in blocco.
3. **Commento sul file, mentre lo si legge** (`corpus_ci/review_comment.py` + `review-comment.yml`,
   installati come `scripts/review_comment.py` + `.github/workflows/review-comment.yml`): in *Files
   changed*, commento **sul file** con `/approve` o `/reject motivo…` (scritto nel corpo del commento, non
   nel riepilogo generale della review). Il workflow applica la decisione **a quel file** (PMID ricavato
   dal path), fa un commit sul branch, risponde nel thread e mette una reazione (👍/😕). Sicurezza: agisce
   solo per autori OWNER/MEMBER/COLLABORATOR, solo su PR dello stesso repo (mai da fork), solo su branch
   `ingest/*`; il testo del commento non è mai eseguito come shell. Parte sia da un commento singolo
   (`pull_request_review_comment`) sia da una review inviata con «Finish your review»
   (`pull_request_review`, che rilegge dall'API tutti i commenti della review); le decisioni sono
   **idempotenti** (ricevute due volte non cambiano nulla né rispondono due volte). **Un `/approve` scritto
   nel campo generale della review (non su un file) non fa nulla**: va scritto sull'icona di commento
   affiancata al file.

**Insidia già incontrata e risolta:** i commenti `/approve` non avviavano il workflow #10 perché i branch
`ingest/*` delle PR erano stati creati **prima** che `review-comment.yml` esistesse su `main`; il
merge-ref della PR restava vecchio e non conteneva il file del workflow (GitHub esegue il file presente
sul branch/ref pertinente, non sempre l'ultima versione di `main`). Fix: PR #11 (trigger anche
`pull_request_review`) **più** un merge di `origin/main` in ogni branch `ingest/*` ancora aperto. Verificato
con un commento di prova della GitHub App: risposta in 20 s. **Regola operativa:** dopo qualunque modifica
ai file `.github/workflows/*.yml` su `main` del repo corpus, aggiornare (merge di `main`, es. "Update
branch" su ciascuna PR) tutti i branch `ingest/*` ancora aperti, altrimenti continuano a usare la versione
vecchia del workflow. I branch creati da run **successivi** a un cambio di workflow lo ereditano
automaticamente (il connettore li crea da `origin/main` aggiornato).

Limite noto: i commit fatti dai workflow con il token del job non riavviano da soli il check `validate`
sulla PR; il modulo «Review documenti» ne rilancia comunque la logica e ne stampa l'esito nel job summary,
ma per aggiornare il check visibile sulla PR serve un "Re-run" o un push. La GitHub App non ha il permesso
*Actions* (letture su `/actions/*` danno 403): per vedere esecuzioni/log dei workflow serve la scheda
Actions su GitHub, non l'API con questa App.

## Scelte prese dove la specifica non bastava (modificabili, elencate nel README)

- `xref` bibliografici/nota/affiliazione rimossi; richiami a tabelle/figure/sezioni **mantengono il testo**.
- Celle unite: valore **ripetuto** nelle celle coperte; intestazioni multi-livello unite con ` / `.
- Contenuto senza titolo dopo l'abstract → `## Main text`. Abstract: JATS principale (mai graphical/toc),
  altrimenti PubMed; se nessuno, `## Abstract` resta vuoto.
- `classification_method`: `source` se `doc_type` viene da un publication type, `rule` se cade su `other`.
- «Renal Dialysis» **non mappato** a `modality` (nel MeSH include HD e PD) → `unspecified`, decide il revisore.
- Ritrattazioni: pt *Retracted Publication*/*Retraction of Publication* **più** link `RetractionIn`.
- Licenze ammesse: CC0, CC BY, CC BY-SA (NC/ND no). Abstract senza full text esclusi. Client httpx proprio,
  non Biopython.
- `parse_document` tollera CRLF (file del corpus modificati su Windows).
- Nomi branch `ingest/<data>-pubmed-<n>`: `n` riparte dal primo numero libero **sul remoto**
  (`git ls-remote`), quindi più run nello stesso giorno non collidono (test in `tests/test_pipeline.py`).

## Insidie già incontrate

- **Windows + percorso lungo**: in cartelle molto profonde le DLL di `cryptography` non si caricano
  ("estensione troppo lunga"). Fix: `UV_PROJECT_ENVIRONMENT` su un percorso corto. In
  `C:\workspace\healthcare-AI\aika_ingestion_pubmed` non dovrebbe servire.
- Console Windows cp1252: usare `PYTHONUTF8=1` per stampare `≤`, `µ`, ecc.
- lxml: `root.append(el)` **sposta** il nodo; nei test copiare con `copy.deepcopy`.
- Il JSON Schema **non può** verificare `id == pmid-<pmid>` né nome file: lo fa `corpus_ci/validate_corpus.py`.
- `git` su Windows con `core.autocrlf=true` produce file CRLF: il codice usa `-c core.autocrlf=false`.
- Negli script di patch generati evitare i doppi backslash (si corrompono): usare Edit o `chr(92)`.
- **GitHub Actions esegue il file di workflow presente sul ref/branch dell'evento**, non sempre l'ultima
  versione di `main`: un branch di PR creato prima di un cambio a un `.github/workflows/*.yml` non lo vede
  finché non viene aggiornato (merge/"Update branch"). Vale anche per `workflow_dispatch` («Use workflow
  from» sceglie letteralmente il ref da cui leggere il file).
- **Repository privato su account GitHub gratuito**: le "rulesets"/branch protection non sono applicate
  ("won't be enforced... until you move to GitHub Team"); un check CI rosso è quindi solo un segnale, non
  un blocco. La GitHub App non ha (e non deve avere) il permesso *Workflows*: il push di file sotto
  `.github/workflows/` richiede le credenziali dell'utente. La GitHub App non ha nemmeno il permesso
  *Actions*: le sue chiamate a `/actions/*` e `/commits/{sha}/check-runs` danno 403.

## Test

`tests/support.py`: `FakeNcbi` (httpx.MockTransport su fixture), repository git temporanei (origin bare +
clone), `FakeHost`. Le fixture JATS in `tests/fixtures/jats/` sono **scritte a mano** (casi difficili:
abstract strutturato, celle unite, tabella dentro `<p>`, figure, liste, senza body, più abstract, licenza NC).
`tests/fixtures/jats_real/` + `pubmed_real/` contengono 3 articoli PMC **reali** CC BY 4.0 (scaricati il
2026-09-26 con `efetch`; test in `tests/test_real_fixtures.py`: tabelle, sup/sub, sezioni multilivello,
determinismo). `pytest-recording` è installato ma non serve: le fixture sono file XML statici.
Fatta una prova di mutazione sul convertitore: le regole di esclusione e di filtro abstract ora sono coperte.

`tests/test_review.py` (comando `review`/`review_branch`), `tests/test_review_script.py` (script
`corpus_ci/review.py`, confrontato byte-per-byte con `review_branch`), `tests/test_review_comment.py`
(script `corpus_ci/review_comment.py`: parsing dei comandi, evento singolo commento, evento review
inviata, idempotenza, guardrail di sicurezza, validità dello YAML del workflow). Questi tre file vanno
tenuti allineati a `models.py` e a `corpus_ci/` se cambia il formato del front matter.

## Da fare / non verificato

- **Revisionare e mergiare le PR aperte #1, #2, #3, #5** (60 documenti `pending`) nel repo corpus, con uno
  dei tre metodi di revisione sopra.
- Query attuale (`config.yaml`): `("Renal Dialysis"[Mesh] OR hemodialysis[TiAb]) AND (Diet/Diet Therapy/
  Nutritional Requirements/Nutritional Status/Nutrition Therapy con [Majr])` + `extra_filter: "pubmed
  pmc[sb]"`. La versione con `[Mesh]`/`[TiAb]` su diet/nutrition portava molti articoli fuori tema (es.
  studi su modelli animali). `max_results: 200` lasciava fuori ~71 delle 271 corrispondenze originarie
  (con il filtro OA attivo, 129 corrispondenze totali): rivedere se serve coprire tutto.
- ~76% dei documenti (campione) cade in `doc_type: other` / tier 5 (manca il publication type NLM):
  valutare se estendere `mapping.publication_type_doc_type` (es. Cross-Sectional/Cohort Studies) o lasciar
  classificare al revisore.
- Gli originali JATS finiscono in `originals/` (locale, gitignorato); `original_uri` è un `file:///`
  locale, quindi non valido su altre macchine: serve storage condiviso (`originals.base_uri`) + upload,
  non implementato.
- Il Markdown delimita le tabelle con righe `::: table id=…` (sintassi non-standard): il chunker deve
  saperle gestire.
- Validare con il clinico il vocabolario `mapping.topics`, le mappe MeSH→modality e gli `evidence_tier`.
- Aggiungere qualche altro articolo PMC OA reale alle fixture, oltre ai 3 già presenti.
- Non implementati (fuori scope v1): PDF, siti web, chunking, embedding, UI di revisione dedicata (oggi si
  usa GitHub stesso), pre-triage LLM, job di controllo retraction sui documenti già approvati,
  `include_abstract_only`, tracciamento strutturato di chi/quando ha revisionato (oggi solo
  `review_notes` + cronologia commit git).
- Valutare se passare a GitHub Team/organizzazione quando i revisori sono più di uno: sul piano gratuito
  attuale non si possono imporre branch protection né limitare chi mergia.

# aika_ingestion_pubmed

Connettore PubMed/PMC per la pipeline RAG sull'alimentazione del paziente in dialisi
(ingestion → chunker → indexing → agent). Cerca su PubMed, scarica **solo il full text
dell'Open Access Subset di PMC**, lo converte in Markdown con front matter YAML (il "contratto"
verso il chunker) e propone i documenti con una **pull request** su un repository corpus
separato, dove un revisore clinico li approva. Il connettore scrive sempre
`curation_status: pending`.

## Requisiti e avvio

- Python 3.13 e [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pre-commit install
uv run pytest && uv run ruff check && uv run ruff format --check && uv run pyright
```

## Configurazione

Tutto è in [`config.yaml`](config.yaml). **Prima del primo run vanno sostituiti i due segnaposto**:
`ncbi.email` (un contatto reale: NCBI lo richiede) e `corpus.repo` / `corpus.local_path`.

I segreti arrivano **solo da variabili d'ambiente**. In locale possono stare in un file `.env` nella
cartella da cui si lancia il comando (già in `.gitignore`, mai da committare): il comando `run` lo carica
con `python-dotenv` senza sovrascrivere le variabili già impostate nell'ambiente.

| Variabile | Uso |
|---|---|
| `NCBI_API_KEY` | opzionale: alza il limite da 3 a 10 richieste/s |
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` | GitHub App che apre le PR |
| `GITHUB_APP_PRIVATE_KEY` oppure `GITHUB_APP_PRIVATE_KEY_PATH` | chiave privata (PEM) della App |

Permessi minimi della GitHub App sul solo repository corpus: **Contents: read & write** (push dei
branch `ingest/…`) e **Pull requests: read & write** (elenco PR aperte, apertura PR). Nient'altro.

Il repository corpus va clonato in `corpus.local_path` con working tree pulito: il connettore legge
lo stato da `origin/main` senza toccare il working tree e crea un branch per ogni PR.

## Uso

```bash
uv run aika-ingestion-pubmed run --dry-run     # cerca e converte, non scrive nulla
uv run aika-ingestion-pubmed run               # run completo: branch, commit, PR
uv run aika-ingestion-pubmed schema            # rigenera schema/front_matter.schema.json
```

Il log è JSON (una riga per evento) su stderr. L'ultimo evento, `run_done`, riporta i conteggi:
trovati, esclusi per motivo, nuovi, aggiornati, invariati, errori, PR aperte. Un errore su un
singolo documento non interrompe il run.

## Flusso di un run

1. `esearch` con la query configurata (`usehistory=y`).
2. `efetch db=pubmed` dei metadati a batch (WebEnv + query_key).
3. Esclusione dei record ritrattati: publication type *Retracted Publication* / *Retraction of
   Publication*, oppure un link `RetractionIn`.
4. Salto dei PMID già rifiutati (`rejected/pmid-<PMID>.md` su `main` del corpus) o già in una PR aperta
   del connettore, in `corpus/` o in `rejected/` (branch `ingest/…`). Nessun full text viene scaricato per questi.
5. `efetch db=pmc` a batch: un articolo è disponibile solo con `<body>` non vuoto.
6. Conversione JATS → Markdown, front matter, `QualityGate`.
7. Confronto del `content_hash` con `main`: uguale → invariato, diverso → *updated*, assente → *new*.
8. PR da massimo 20 documenti ordinati per `evidence_tier`, un commit ciascuna, descrizione con
   tabella (id, titolo, `doc_type`, `modality`, `topics`, stato, link PubMed).

### Motivi di esclusione (chiavi dei conteggi)

`retracted`, `rejected_in_corpus`, `in_open_pr`, `no_pmcid`, `no_open_access_fulltext`,
`error_read_corpus`, `quality:language_missing|language_not_allowed|license_missing|
license_not_allowed|body_too_short|schema_invalid`.

## Revisione: dalla PR al corpus

`main` del repository corpus contiene **solo documenti già revisionati**. Il connettore apre PR con
documenti `pending`; il revisore decide **dentro la PR**, file per file, e poi mergia:

```bash
# approvare (restano in corpus/, curation_status: approved)
uv run aika-ingestion-pubmed review approve 40549189 42739048 --branch ingest/2026-09-26-pubmed-5
uv run aika-ingestion-pubmed review approve --all-pending --branch ingest/2026-09-26-pubmed-5
# rifiutare (spostati in rejected/, curation_status: rejected, motivazione obbligatoria)
uv run aika-ingestion-pubmed review reject 42087061 --notes "fuori tema" --branch ingest/2026-09-26-pubmed-5
```

Da GitHub, senza riga di comando: **Actions → «Review documenti» → Run workflow**: nel menu a tendina «Use workflow from» si sceglie il
branch della PR (`ingest/…`), poi si compilano decisione (`approve`/`reject`), PMID (o `all-pending` per
approve) e motivazione. Il file del workflow deve esistere sul branch scelto: i branch nati prima del merge
vanno aggiornati con «Update branch». Il workflow
(`corpus_ci/review.yml` + `review.py`, installati nel repo corpus; lanciabili solo su branch `ingest/*`) fa
un commit sul branch della PR e mostra lo stato residuo. Nota: i commit dei workflow non riavviano la CI
della PR; «Stato residuo» rieseguisce la validazione. **Mentre leggi un documento:** in *Files changed* commenta il file con `/approve` oppure
`/reject motivo…` (workflow `corpus_ci/review-comment.yml` + `review_comment.py`). La decisione vale per quel
file; il workflow fa il commit sul branch, risponde nel thread e mette una reazione. Solo OWNER/MEMBER/
COLLABORATOR, PR dello stesso repo, branch `ingest/*`.
Ogni comando `review` crea un commit sul branch
della PR e lo pusha (serve la GitHub App). Il corpo del documento
non cambia, quindi `content_hash` resta valido. La CI del corpus (`corpus_ci/`) è rossa finché nella PR
resta un file `pending` in `corpus/`: si mergia solo a verde. I file in `rejected/` non sono mai letti dal
chunker e il connettore non riscarica quei PMID. I documenti dubbi (`suspended`) si lasciano fuori dalla PR.

## Vincoli NCBI rispettati

- Solo E-utilities `esearch.fcgi` / `efetch.fcgi` (`db=pubmed`, `db=pmc`); `EutilsClient` rifiuta
  qualsiasi altro endpoint. Nessun uso di `oa.fcgi` né dell'FTP PMC.
- Throttling client-side (3/s, 10/s con API key), retry con backoff esponenziale (1, 2, 4… s, tetto
  60 s, `Retry-After` rispettato) su 429, 5xx ed errori di rete.
- `tool` ed `email` in ogni richiesta. Le richieste sono sempre POST: `api_key` sta nel corpo, mai
  in un URL, quindi nemmeno in messaggi d'errore o log.

## Contratto del file Markdown

`corpus/pmid-<PMID>.md`: front matter YAML (chiavi in ordine fisso, opzionali vuote omesse, PMID
sempre stringa) + corpo. `content_hash` è lo `sha256:` del **solo corpo**, quindi `retrieved_at` non
genera diff. Il modello è [`models.py`](src/aika_ingestion_pubmed/models.py) (Pydantic v2);
da esso si genera [`schema/front_matter.schema.json`](schema/front_matter.schema.json) per la CI del
corpus (un test e l'hook pre-commit ne verificano l'allineamento).

> **Limite noto del JSON Schema.** Non può esprimere che `id` sia `pmid-<pmid>` né che coincida
> col nome del file. La CI del repository corpus deve verificarlo a parte (Pydantic lo fa già).

Corpo: un solo `# titolo`, `## Abstract` sempre presente (sottosezioni `###` se strutturato),
sezioni del `<body>` con i titoli originali, un paragrafo per riga, tabelle come
`::: table id=…` e figure (solo didascalia) come `::: figure id=…`, apici/pedici come `<sup>`/`<sub>`,
bibliografia/affiliazioni/finanziamenti/conflitti/ringraziamenti/supplementari esclusi, Unicode NFC,
serializzazione deterministica.

## Scelte di implementazione da conoscere

Dove la specifica lasciava spazio o non bastava, queste sono le scelte fatte (tutte modificabili):

- **`xref`**: i richiami bibliografici (`bibr`), di nota, affiliazione e autore sono rimossi con
  pulizia di parentesi e virgole orfane. I richiami a tabelle, figure e sezioni **mantengono il
  testo** («see Table 2» non diventa «see»).
- **Celle unite**: Markdown non ha `colspan`/`rowspan`; il valore viene **ripetuto** in ogni cella
  coperta, così ogni riga è autosufficiente. Le intestazioni su più livelli sono unite con ` / `.
- **Contenuto senza titolo dopo l'abstract** (paragrafi prima della prima sezione): riceve
  `## Main text`, altrimenti finirebbe sotto «## Abstract».
- **Abstract**: si usa quello JATS principale (mai `graphical`, `toc`, `teaser`…); se manca, quello
  PubMed. Se non esiste nessuno, l'intestazione resta con la sezione vuota.
- **`classification_method`**: `source` quando `doc_type` deriva da un publication type di PubMed,
  `rule` quando cade su `other`.
- **`modality`**: «Renal Dialysis» è volutamente non mappato (nel MeSH include emodialisi e dialisi
  peritoneale) → `unspecified`, la conferma spetta al revisore. `ckd_non_dialysis` è scartata se
  compaiono termini di dialisi.
- **`suspended`**: solo un PMID in `rejected/` è bloccato; un documento `suspended` che cambia upstream
  torna in revisione. In un aggiornamento `review_notes` viene conservato e lo stato torna `pending`.
- **CRLF**: i file del corpus modificati su Windows (fine riga CRLF) sono letti correttamente.
- **Originali XML**: v1 = directory locale (`originals.dir`); `original_uri` usa `originals.base_uri`
  (es. `gs://bucket/pubmed`) oppure l'URI `file://`.

## Decisioni aperte (default adottati)

| Tema | Default in v1 |
|---|---|
| Abstract senza full text OA | esclusi (`include_abstract_only` non implementato) |
| Licenze | CC0, CC BY, CC BY-SA. CC BY-NC / CC BY-ND **non** ammesse: si aggiungono in `quality.allowed_licenses` |
| Filtro OA in ricerca | non attivo: `search.extra_filter` è pronto, va verificata la sintassi e che non perda risultati |
| Biopython vs httpx | client httpx proprio (sottile, con throttling e retry testati); il parsing JATS è comunque nostro |

## Test

`uv run pytest` — nessun test contatta NCBI (`--block-network`; NCBI e GitHub sono simulati con
`httpx.MockTransport`). Il flusso completo gira su repository git temporanei reali (origin + clone).

Le fixture JATS in `tests/fixtures/` sono **scritte a mano** con la struttura dei casi difficili
(abstract strutturato, celle unite, tabella dentro un paragrafo, figure, liste, articolo senza
`<body>`, più abstract, licenza NC). Prima di fidarsi in produzione conviene aggiungere anche
qualche articolo reale scaricato da PMC OA e registrarlo con `pytest-recording`
(`uv run pytest --record-mode=once`, poi rimettere `--block-network`).

## Struttura

```
src/aika_ingestion_pubmed/
  eutils.py         EutilsClient (rate limit, retry, parametri comuni)
  search.py         PubMedSearch (esearch + efetch metadati a batch)
  pubmed_parser.py  PubMedMetadataParser
  pmc.py            PmcFullTextFetcher (+ estrazione licenza)
  jats.py           JatsToMarkdown           inline.py  rendering inline condiviso
  builder.py        FrontMatterBuilder       licenses.py  normalizzazione licenze
  quality.py        QualityGate              document.py  serializzazione deterministica
  corpus.py         CorpusWriter (git)       github.py  GitHub App
  pipeline.py       flusso di un run         cli.py, schema.py, config.py, models.py
```

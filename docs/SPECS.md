# lazy-bootstrap — specifiche e diario delle decisioni di design

> Documento vivo. Ogni scelta strutturale è registrata come **D-nn** con: contesto,
> alternative realmente considerate, strada presa e motivazione. Serve per poterci
> tornare sopra fra sei mesi e capire *perché*, non solo *cosa*.
>
> Convenzione: `status` = `adottata` | `provvisoria` (da rivedere quando cade un
> vincolo) | `scartata`.

---

## 0. Obiettivo

Sistema per fare il **bootstrap di immagini di distro comuni** con **ricompilazione
opzionale** dei pacchetti e **opzioni extra** non previste dai bootstrap standard
(`debootstrap`, `apk`, `mmdebstrap`, ...).

Primo passo (questo repo, questa iterazione): dato un riferimento a un'immagine,
ricavare l'elenco dei pacchetti installati dal package manager, **ricompilarli tutti**
dai sorgenti, potendo **sostituire la toolchain** e applicare opzioni extra, su
**host / sandbox / container / VM**, con **reporting** e **confronto fra esecuzioni**.

Caso d'uso concreto di riferimento:

| immagine | toolchain da confrontare |
|---|---|
| `docker.io/library/debian:13-slim` | `gcc` (default), `llvm`, `llvm-20.1.8`, `filc` |
| `docker.io/library/alpine` + `libc6-compat` | idem, con variante musl di Fil-C |

---

## 1. Decisioni trasversali

### D-01 — Linguaggio del driver: Python 3.11 stdlib-only (+ gemello Bash)
* **Alternative**
  1. Tutto in Bash/POSIX sh.
  2. Tutto in Python con dipendenze (pyyaml, jinja2, rich).
  3. Go/Rust compilato.
  4. Python stdlib-only per il driver, con uno **script Bash equivalente** per la pipeline.
* **Presa: (4).**
* **Perché** — Il reporting (JSON canonico, HTML autoconsistente, confronto fra run,
  statistiche) in Bash diventa illeggibile: è esattamente il tipo di codice che Python
  rende banale. Al contrario, la *pipeline* (inventario → toolchain → build → risultato)
  in Bash è leggibilissima ed è la parte che un umano vuole poter **ripercorrere a mano**.
  Le dipendenze esterne sono escluse perché il tool deve girare dentro immagini minimali
  e in CI senza rete verso PyPI. Un binario compilato aggiungerebbe un bootstrap
  (compilare il compilatore-di-compilatori) che è proprio ciò che vogliamo evitare.
* **Conseguenza** — I due gemelli (`lazy-bootstrap` Python, `sh/lazy-bootstrap.sh` Bash)
  devono produrre **lo stesso JSON** (`docs/report-schema.md`). Vedi D-14.

### D-02 — Il target deve avere solo `/bin/sh`
* **Alternative** — (a) installare Python nel target; (b) parlare al target solo via `sh`.
* **Presa: (b).**
* **Perché** — `debian:13-slim` e `alpine` non hanno Python e in questa iterazione non
  vogliamo download extra. Tutta l'intelligenza sta sul driver (host); verso il target
  passano **solo stringhe di shell POSIX**. Effetto collaterale gradito: lo stesso codice
  funziona su host, bwrap, firejail, container e (in futuro) VM via ssh, perché l'unico
  requisito è "sai eseguire uno script sh e riportare rc/stdout/stderr".

### D-03 — Formato di configurazione: TOML
* **Alternative** — YAML (serve PyYAML), JSON (niente commenti), TOML (`tomllib` in stdlib).
* **Presa: TOML.** Commentabile, in stdlib dal 3.11, sufficiente per profili piatti.
  I workflow GitHub restano YAML perché lo impone GitHub.

---

## 2. Esecuzione: il livello `Executor`

### D-04 — Astrazione unica `Executor` con 4 backend
* **Interfaccia** — `run(script) -> CommandResult`, `upload`, `download`, `read_text`,
  `write_text`, `which`. Volutamente minima: è il minimo comune denominatore fra
  processo locale, namespace, container e (futuro) VM.
* **Backend implementati** — `host`, `bwrap`, `firejail`, `oci` (podman **o** docker).
* **Perché non un'interfaccia più ricca** — Ogni metodo in più va implementato 4 volte
  e crea divergenze. Copiare file e lanciare shell bastano per tutto il resto
  (es. "esiste il file X?" = `test -e X`).
* **Costo accettato** — Niente streaming incrementale dell'output: si raccoglie a fine
  step. In cambio la logica di ogni backend sta in ~100 righe leggibili.

### D-05 — `oci` come backend di default, non `host`
* **Perché** — Ricompilare centinaia di pacchetti installa build-dep a valanga:
  farlo sull'host distrugge l'host. Il container è l'unico backend in cui
  "riparti pulito" costa zero. `host` resta per debug rapido e per chi sa cosa fa.

### D-06 — Sessione lunga + `exec`, non un container per comando
* **Alternative** — (a) `podman run` per ogni comando; (b) un container `sleep infinity`
  e `podman exec` per ogni step.
* **Presa: (b).** Lo stato (build-dep installate, sorgenti scaricati) deve sopravvivere
  fra gli step. (a) costringerebbe a ricostruire un layer per step: lento e fragile.

### D-07 — `bwrap`/`firejail` richiedono un rootfs su disco
* Il rootfs si materializza da un'immagine OCI con, in ordine di preferenza:
  `podman export` di un container creato → `docker export` → `skopeo copy` + `umoci unpack`.
* **Perché quest'ordine** — `podman export` è il percorso con meno pezzi mobili quando
  podman c'è già (ci serve comunque per il backend `oci`). `skopeo`+`umoci` è il
  fallback puro-OCI per host senza engine.
* **Nota firejail** — `firejail --chroot` è più fragile di bwrap (rifiuta alcune
  combinazioni, e da root ha limitazioni note). È implementato e verificato da
  `lazy-bootstrap doctor`, ma **non** è il default.

---

## 3. Distribuzioni

### D-08 — `Distro` come driver di *famiglia*, non di release
* `debian.py` copre tutta la famiglia apt/dpkg (Debian, Ubuntu, derivate);
  `alpine.py` copre apk/abuild.
* **Perché** — Le differenze fra Debian 13 e Ubuntu 24.04 sono dati (URL dell'archivio,
  nome della suite), non codice. Trattarle come dati rende banale puntare il driver
  Debian a un archivio diverso, che è esattamente ciò che serve quando la rete è
  filtrata (vedi D-20).

### D-09 — Inventario letto dal DB del package manager, non dal registry
* `dpkg-query -W` / `/lib/apk/db/installed`.
* **Alternative scartate** — leggere i layer OCI e dedurre i pacchetti; interrogare
  l'archivio remoto. Entrambe richiedono rete e indovinano; il DB nell'immagine è
  la verità, è già lì, e costa un `exec`.
* **Bonus** — `dpkg-query` riporta anche `source:Package`/`source:Version`, quindi la
  deduplicazione binario→sorgente (26 binari da 1 sorgente) è gratis.

### D-10 — Unità di ricompilazione = **pacchetto sorgente**
* **Perché** — Ricompilare `libc6` e `libc6-dev` separatamente è impossibile: nascono
  dallo stesso `dpkg-buildpackage`. Si costruisce per sorgente e si mappano indietro
  i binari. Questo riduce anche il lavoro di ~3x su un'immagine slim.

---

## 4. Toolchain

### D-11 — Iniezione della toolchain via **directory di shim in `PATH`**, non solo `CC`/`CXX`
* **Alternative**
  1. Solo `CC`/`CXX` (+ `DEB_*_APPEND` su Debian).
  2. Riscrivere `debian/rules` / `APKBUILD` per pacchetto.
  3. Directory di wrapper (`cc`, `gcc`, `g++`, `c++`, `x86_64-linux-gnu-gcc`, `ld`, ...)
     messa in testa a `PATH`, che rimanda al compilatore scelto con i flag extra.
* **Presa: (3), con (1) in aggiunta.**
* **Perché** — Moltissimi build system ignorano `CC` (Makefile scritti a mano, autotools
  con cache, cmake che ricorda il compilatore). Lo shim è l'unico meccanismo che li
  cattura tutti senza toccare i sorgenti, ed è anche il punto naturale in cui iniettare
  le "opzioni extra" richieste. (2) non scala e sporca il pacchetto.
* **Costo accettato** — Un pacchetto che invoca il compilatore per path assoluto
  (`/usr/bin/gcc`) sfugge allo shim. È raro; il report lo mostra come "compilato con
  la toolchain sbagliata" solo se il pacchetto lo dichiara, quindi resta un limite noto.

### D-12 — Tre strategie di provisioning, ordinate e con fallback: `distro` → `binary` → `source`
* **Perché tutte e tre** — È il requisito: la stessa toolchain può venire dal package
  manager (veloce, versione della distro), da un tarball di release upstream (versione
  esatta, nessuna compilazione) o dai sorgenti (qualsiasi versione, costo alto).
  L'ordine è configurabile per toolchain, così `llvm` = "quello che c'è" e
  `llvm-20.1.8` = "esattamente questo, scaricalo se la distro non ce l'ha".
* **Regola sulla versione** — se è richiesta una versione esatta e la distro offre solo
  una vicina, `distro` **fallisce di proposito** e si passa a `binary`; la versione
  effettivamente usata è sempre registrata nel report, mai silenziosamente sostituita.

### D-13 — Fil-C: selezione della variante in base alla **libc del target**
* Fil-C pubblica due artefatti per release:
  * `filc-<v>-linux-x86_64.tar.xz` — distribuzione "pizfix", **musl**, self-contained,
    non richiede root;
  * `optfil-<v>-linux-x86_64.tar.xz` — distribuzione `/opt/fil`, **glibc 2.40**, richiede root.
* **Presa** — `variant = auto` sceglie in base alla libc rilevata nel target
  (`/lib/ld-musl-*` → pizfix; `ld-linux-*` → optfil), con override esplicito.
* **Caso Alpine + `libc6-compat`** — `libc6-compat`/`gcompat` è uno *shim*, non glibc:
  l'immagine resta musl, quindi `auto` sceglie **pizfix**. Documentato perché è
  controintuitivo e il requisito lo chiedeva esplicitamente.
* **`setup.sh` di pizfix richiede `patchelf`** — se manca nel target si evita
  `patchelf` e si esporta `LD_LIBRARY_PATH` verso `pizfix/lib`: stesso effetto,
  zero dipendenze. (Verificato: i binari risolvono già le librerie via `$ORIGIN`.)
* **Versione LLVM** — Fil-C 0.681 è basato su **clang 20.1.8**, quindi la famiglia
  `filc-*` segue le release Fil-C, non quelle LLVM.

---

### D-22 — Flag policy: lo shim può *togliere* flag, non solo aggiungerne
* **Problema osservato** (non ipotetico: emerso alla prima build reale). Debian/Ubuntu
  passano di default `-flto=auto -ffat-lto-objects`. Fil-C non distribuisce il plugin
  LTO, quindi *ogni* link fallisce con
  `ld: LLVMgold.so: error loading plugin`. Il compilatore funziona benissimo: è la
  distro che gli chiede una cosa che non ha.
* **Alternative**
  1. Lasciar fallire e registrare il fallimento.
  2. Patchare i pacchetti.
  3. `drop_flags`: lo shim filtra dalla command line i pattern che quella toolchain
     non può onorare, con default per driver e override per run.
  4. Solo il meccanismo nativo della distro (`DEB_BUILD_OPTIONS=nolto`).
* **Presa: (3) + (4) insieme.**
* **Perché** — (1) produce un report in cui il 100% dei pacchetti "fallisce" per un
  motivo che non è il codice: informazione zero, e falsa il confronto fra toolchain,
  che è lo scopo del tool. (2) non scala. (4) da solo copre solo i pacchetti che
  passano da `dpkg-buildflags`, e non esiste su Alpine. (3) è l'unico punto che
  cattura tutto, ed è lo stesso punto in cui già iniettiamo le opzioni extra: la
  simmetria "aggiungi flag / togli flag" rende il meccanismo uno solo.
* **Default** — `filc` toglie i flag LTO. `gcc` e `llvm` non tolgono niente: il clang
  della distro digerisce i flag della distro (verificato).
* **Visibilità** — i flag rimossi sono scritti nel commento in testa allo shim, nelle
  note del report, e stampati a ogni invocazione con `LB_SHIM_TRACE=1`. Una modifica
  silenziosa della command line sarebbe inaccettabile in uno strumento di misura.

### D-23 — Toolchain con runtime proprio: si rilassa il *packaging*, non la build
* **Problema osservato** — con Fil-C, compilazione e link riescono, poi
  `dpkg-shlibdeps` fallisce: il binario dipende da `libpizlo.so`, `libc.so.6666`,
  ... che non appartengono a nessun `.deb`, quindi non è possibile calcolare le
  dipendenze del pacchetto.
* **Alternative**
  1. Considerarlo un fallimento del pacchetto.
  2. `LD_LIBRARY_PATH` globale verso il runtime della toolchain.
  3. Uno shim per `dpkg-shlibdeps` che aggiunge `-l<runtime>` e `--ignore-missing-info`.
* **Presa: (3).**
* **Perché** — (1) è falso: il codice compila, e la domanda a cui il tool risponde è
  "questo sorgente si ricompila con questa toolchain", non "il `.deb` risultante è
  conforme alla policy Debian" (non può esserlo: linka un'altra libc). (2) è
  deprecato da dpkg stesso e soprattutto avvelenerebbe *ogni* processo della build,
  incluso `make` e `sh`, che si troverebbero una libc estranea in testa al percorso
  di ricerca. (3) tocca esattamente il programma che ha il problema, con la stessa
  tecnica già usata per il compilatore (D-11): un solo meccanismo da capire.
* **Attivazione** — automatica e solo quando la toolchain dichiara `runtime_libdirs`,
  cioè quando si porta dietro un runtime che la distro non impacchetta. gcc e clang
  della distro non lo dichiarano e restano in regime stretto.

### D-24 — Le dipendenze di sistema si dichiarano in `ci/system-deps/`, non nel codice
* **Problema osservato** — Fil-C ha bisogno di `xz` nel target per scompattare il
  proprio archivio. La prima soluzione (scompattare sull'host e caricare un `.tar`)
  era un workaround nascosto nel driver.
* **Alternative**
  1. Workaround nel driver.
  2. Elenco di pacchetti hardcoded nel driver Python.
  3. File di testo per (famiglia distro × componente), letti sia da
     `lazy-bootstrap` a runtime sia dai `Containerfile` dei flavour.
* **Presa: (3).**
* **Perché** — Il requisito "l'immagine di build del flavour fil-c contiene xz" e il
  requisito "l'ambiente creato al volo contiene xz" sono lo stesso requisito: se
  vivono in due posti divergono, e divergeranno proprio quando serve. Un file di
  testo è leggibile, diffabile e commentabile; il `Containerfile` lo `COPY`a e lo
  installa con lo stesso comando che usa il runtime.
* **Politica di installazione** — `--system-deps off | target | host`.
  Il default `target` installa dentro container/sandbox (usa e getta). Sul backend
  `host` installare pacchetti significa modificare la macchina di chi lavora: serve
  `--system-deps host` esplicito, altrimenti si annota cosa *non* è stato installato
  e si prosegue. Rifiutarsi e basta sarebbe inutilmente rigido; installare di
  nascosto sarebbe inaccettabile.
* **Il workaround resta, ma come fallback dichiarato** — se `xz` manca comunque
  (`--system-deps off`, archivio distro irraggiungibile, immagine precotta che l'ha
  tolto), si scompatta sull'host **con un warning**. Mai in silenzio.

### D-25 — Ambiente di esecuzione = backend × sorgente del rootfs (assi ortogonali)
Il requisito è coprire host puro, chroot e sandbox, con il filesystem che può venire
dalla macchina stessa o dal pool di immagini. Trattarli come un'unica enumerazione
avrebbe prodotto una combinatoria di casi speciali.

* **Asse 1 — backend (come si esegue)**: `host`, `chroot`, `bwrap`, `firejail`, `oci`
  (podman/docker). Interfaccia unica `Executor` (D-04).
* **Asse 2 — rootfs (da dove viene il filesystem)**: gestito da `rootfs.py`,
  indipendente dal backend.

| sorgente | significato | note |
|---|---|---|
| `image[:REF]` | scompattata dal pool locale (podman/docker, o skopeo+umoci) | default |
| `hostfs[:MODE]` | derivata dal filesystem dell'host | `overlay` (copy-on-write, default se root+overlayfs), `bind` (sola lettura, costo zero), `copy` (copia vera, funziona ovunque) |
| `dir:PATH` | directory preparata da altri | con `--rootfs-prepare CMD` la creazione è **delegata**: debootstrap, mmdebstrap, un altro engine, un artefatto di CI |
| `none` | nessun rootfs | il backend `host` |

* **Asse 3 — percorsi**: sia il rootfs (`--rootfs-path`) sia la directory di lavoro
  (`--workdir`) accettano *default* (sotto la cache), *percorso esplicito*, oppure
  `temp` (creata e rimossa a fine run).
* **Perché ortogonali** — "chroot su un'immagine", "bwrap su un overlay dell'host",
  "chroot su un rootfs fatto da debootstrap" sono tre combinazioni degli stessi due
  assi. Con l'enumerazione sarebbero tre implementazioni; così sono zero righe di
  codice in più.
* **`hostfs:overlay` è il modo interessante** — dà un chroot che *è* la macchina
  corrente ma in cui ogni scrittura finisce in un layer separato: si ricompila
  contro l'ambiente reale senza sporcarlo. È il motivo per cui `overlay` è il default
  di `auto`, con `copy` come ripiego dove overlayfs non c'è.
* **`chroot` accanto a `bwrap`** — isolamento più debole (niente namespace: PID, rete
  e utenti sono quelli dell'host) ma richiede solo root e `chroot(8)`. È il backend
  che funziona sulle macchine più vecchie e più bloccate; `bwrap` resta preferibile
  quando c'è.
* **firejail** — `--chroot` è disabilitato di default su Debian/Ubuntu
  (`/etc/firejail/firejail.config`). Il tool lo rileva e dice esattamente la riga da
  aggiungere, invece di fallire a ogni step. Non modifica la configurazione da solo:
  è una scelta di sicurezza della macchina, non nostra.

### D-26 — Costruire il worker e ricompilare sono lo stesso lavoro, su qualsiasi classe
Requisito: sia la **costruzione dell'immagine worker** sia l'**esecuzione della
ricompilazione** devono poter girare su una qualunque delle classi supportate.

* **Alternative**
  1. Le immagini si costruiscono con `podman build`/`docker build` (Containerfile),
     la ricompilazione gira dove vuole.
  2. Un builder astratto che esegue gli stessi passi su qualsiasi backend, con
     persistenza del risultato scelta a parte.
* **Presa: (2), mantenendo (1) come scorciatoia.**
* **Perché** — "installa le dipendenze dichiarate + provisiona la toolchain" è
  esattamente ciò che `prepare_builder` + `Toolchain.provision` già fanno, e lo fanno
  su `Executor`. Legarlo a `podman build` significherebbe riscriverlo in Containerfile
  e mantenerlo due volte (e infatti divergerebbe: vedi D-24). Con (2), su una macchina
  senza engine si costruisce comunque un worker — in un chroot o in bwrap — e lo si
  usa. I `Containerfile` in `ci/images/` restano perché in CI `podman build` dà cache
  a layer e push al registry gratis, ma installano **le stesse liste** di
  `ci/system-deps/`.
* **Assi separati: dove si costruisce ≠ come si conserva.**

  | classe di build | persistenza possibile |
  |---|---|
  | `oci` (podman/docker) | `image:` (commit), `dir:` (export), `tar:` |
  | `chroot`/`bwrap`/`firejail` | `dir:` (il rootfs stesso), `tar:`, `image:` (import) |
  | `host` | `dir:` esplicito |

  Un worker costruito in chroot e salvato come directory si riusa con
  `--rootfs dir:<path>`; salvato come immagine, con `--backend podman`. La classe di
  costruzione non vincola la classe di esecuzione.
* **Il worker si autodescrive** — `/opt/lazy-bootstrap/worker.json` dentro l'ambiente
  registra flavour, distro, libc, toolchain e da dove è stata presa. Un rootfs o
  un'immagine trovati sei mesi dopo dicono da soli cosa sono.

#### Note operative emerse testando le classi (tutte verificate, non ipotetiche)
* **bwrap**: ogni `run()` è un processo `bwrap` separato, quindi un `--tmpfs /tmp`
  sarebbe **vuoto a ogni step**. Lo stato di sessione è il rootfs: niente tmpfs, e
  `/tmp` del rootfs a 1777 (apt scende a `_apt` e deve poterci scrivere).
* **firejail**: rifiuta un chroot con `/run` scrivibile da tutti, e `--caps.drop=all`
  + `--nogroups` rompono `setgroups(2)`, quindi ogni `apt-get install` fallisce con
  "Permission denied". Il backend quindi **non** droppa quelle capability: il confine
  è il chroot, e chi vuole di più passa `LB_FIREJAIL_ARGS`.
* **chroot / firejail**: il `/etc/resolv.conf` di un'immagine è un segnaposto che
  l'engine sostituisce a runtime; senza engine il DNS è morto. Si fa **bind** di
  quello dell'host (e si smonta a fine sessione) invece di copiarlo, così il rootfs
  in cache non viene modificato.
* **La cache dei rootfs è condivisa fra backend**: un permesso cambiato da uno può
  rendere il rootfs inaccettabile per un altro. È il motivo per cui i backend toccano
  il minimo indispensabile.

### D-27 — L'orchestrazione dell'ambiente è un componente indipendente
Requisito: la parte che gestisce gli ambienti di esecuzione diventerà **una repo
separata** e farà da interfaccia per altri sistemi e script.

**La domanda posta:** la pipeline è (1) genera/verifica le immagini builder e poi
(2) esegue la ricompilazione nell'ambiente scelto? Oppure il punto (1) non è una
fase separata, perché lo script usa l'interfaccia per tutto e al massimo *interroga*
l'orchestratore sulla disponibilità, con un default in caso di assenza
(`--download-image` / `--no-download-image`)?

**Risposta: la seconda.** Il punto (1) non è una fase.

* **Alternative**
  1. Due fasi esplicite: prima si costruiscono le immagini, poi si ricompila.
  2. Fase unica: il job **dichiara il fabbisogno**, l'orchestratore **risolve**
     (già disponibile? scaricare? costruire?) sotto una **policy** scelta dal
     chiamante ma applicata dall'orchestratore.
* **Presa: (2).**
* **Perché** — Con (1) la conoscenza di "quali immagini servono" finirebbe in due
  posti: nello script che le pre-costruisce e nel job che le usa. Divergerebbero
  esattamente quando cambia una toolchain. Peggio: il chiamante dovrebbe
  implementare la logica di acquisizione (pull? unpack? build?) che è precisamente
  il mestiere dell'orchestratore. Con (2) il job dice *cosa gli serve* e *cosa è
  disposto a lasciar fare* (`acquire`), mai *come*.
* **Chi decide cosa serve** — il **dominio**, non l'orchestratore: un flavour
  (`ci/flavours.toml`) è distro × toolchain, e da lì discendono immagine base e
  dipendenze. L'orchestratore non sa cosa sia una toolchain e non deve saperlo.
  L'orchestratore risponde solo a "questo ambiente, ce l'ho? cosa costa averlo?".

#### Il contratto
```
probe()               cosa sa fare questa macchina
available(request)    potresti darmi questo, e cosa costerebbe?   (nessun effetto)
open(request)         dammelo (acquisendolo se la policy lo permette)
```
`available()` è **senza effetti collaterali** — è l'"interrogare l'orchestratore"
della domanda — e c'è un test che lo verifica. `open()` esegue ciò che
`available()` aveva previsto.

#### La policy di acquisizione
| `--acquire` | l'orchestratore può |
|---|---|
| `require` (`--no-download-image`) | solo usare ciò che è già presente |
| `download` (`--download-image`) | scaricare da un registry |
| `build` | costruire in locale da una ricetta |
| `auto` (default) | scaricare se manca; non costruire |

Il pre-riscaldamento resta **possibile ma facoltativo**: `lazy-bootstrap env ensure`
acquisisce in anticipo (utile in CI per separare "scarica" da "compila" nei tempi
di job), ma nessun comando lo richiede.

#### Confine e regola
`src/lazybootstrap/orchestration/` non importa **nulla** dal resto del progetto —
verificato da un test, non solo per convenzione. Ciò che di volta in volta sembra
servirgli dal dominio va passato dentro `EnvironmentRequest`. Il giorno dello
scorporo, il pacchetto si sposta così com'è e questa repo lo consuma come
dipendenza.

**Anche `host` passa dall'interfaccia**, pur non avendo nulla da acquisire.
Trattarlo come caso speciale creerebbe due percorsi di codice che divergerebbero;
così è semplicemente il provider banale.

**Cosa resta al progetto vero e proprio**, come da intuizione nella domanda: la
conoscenza di dominio (inventario, sorgenti, iniezione della toolchain, build,
report) più le chiamate all'interfaccia. Non è però "un set minimo di script per
l'host + invocazioni all'orchestratore per il resto": il dominio non distingue
l'host dagli altri, perché non ne ha motivo.

### D-28 — Classe VM: interfaccia generica, un solo driver reale
Requisito: VM bare qemu/kvm e libvirt, con interfaccia generica per altri engine
(VirtualBox, cloud, ...), ma **supporto effettivo solo per qemu/kvm**.

* **Cosa cambia rispetto alle altre classi** — Nulla, sul contratto: `Executor`
  chiede "esegui uno script sh" e "sposta file", e una VM lo soddisfa come un
  container. Cambia il **trasporto**: i comandi devono attraversare il confine.
  * `ssh` — richiede sshd nel guest; è la risposta generale, ed è anche ciò che
    servirà per libvirt, per una VM remota e per un'istanza cloud. È il trasporto
    implementato.
  * condivisione di directory (virtio-9p) — predisposta per guest senza sshd.
* **Il seam per gli altri engine è `VmDriver`**: tre metodi (`available`, `start`,
  `stop`). Un driver deve solo avviare un guest e dire come raggiungerlo; tutto il
  resto (shell, copia file, tracciamento) è già scritto una volta sola.
* **I driver non implementati sono elencati lo stesso** (`libvirt`, `virtualbox`,
  `cloud`): `--engine libvirt` risponde "non ancora implementato, ecco l'interfaccia"
  invece di "opzione sconosciuta". Sono due informazioni diverse per chi legge.
* **Niente acquisizione implicita di un disco.** Per le altre classi `--acquire auto`
  può scaricare un'immagine OCI; per la VM no, e di proposito: *un'immagine container
  non è un disco avviabile*. Se `--image` punta a un riferimento tipo
  `debian:13-slim`, `available()` lo dice esplicitamente invece di provare a
  scaricarlo e fallire più tardi in modo oscuro.
* **KVM è un'ottimizzazione, non un requisito**: `accel=auto` usa KVM se c'è
  `/dev/kvm`, altrimenti TCG (emulazione: corretta, molto più lenta) e lo dichiara
  con un warning. Rifiutarsi senza KVM renderebbe la classe inutilizzabile proprio
  negli ambienti in cui serve di più (CI, container senza virtualizzazione annidata).
* **Sonda di prontezza: il banner SSH, non la porta.** La rete user-mode di qemu
  accetta la connessione sulla porta inoltrata dall'istante in cui parte e solo
  dopo la resetta: un `connect()` riuscito non significa niente. Il primo byte che
  può arrivare solo da un sshd vivo è `SSH-`. (Sbagliato al primo tentativo,
  corretto dopo averlo osservato.)

#### Stato della validazione (onesto)
| aspetto | stato |
|---|---|
| interfaccia `VmDriver` e registro dei driver | implementata, coperta da test |
| `probe()` / `available()` per la classe VM | implementati e verificati |
| costruzione della riga di comando qemu | verificata (qemu la accetta e riporta errori propri, es. il lock del disco) |
| **boot completo di un guest** | **non verificato in questo ambiente** |

L'ambiente di sviluppo non ha `/dev/kvm` né virtualizzazione annidata, quindi qemu
può girare solo in TCG; e l'esecuzione prolungata di qemu viene terminata dalla
sandbox (exit 144) dopo poche decine di secondi, ben prima che un guest Ubuntu
emulato completi il boot. Su una macchina con KVM il percorso è quello standard
(cloud image + seed cloud-init + inoltro di porta), ed è documentato in
`tests/matrix.sh` come caso attivabile con `LB_TEST_VM_IMAGE`.

Non è stato aggiunto un finto "successo" per questa classe: un caso di test che
non ha mai eseguito il boot deve risultare *skip*, non *pass*.

### D-29 — Non esportare `CC`/`CXX` accanto allo shim (correzione)
Esportavo `CC`/`CXX` *insieme* allo shim in `PATH`, come "belt-and-braces". È
sbagliato, e lo è in un modo che **falsava la baseline**.

`gzip` costruisce anche `gzip.exe` per il pacchetto `gzip-win32`, con un
sotto-configure `--host=i686-w64-mingw32`. Autoconf, se trova `CC`
nell'ambiente, lo usa **al posto** del compilatore derivato da `--host`.
Riproduzione minima, con le mingw regolarmente installate:

| ambiente | `CC` scelto da configure | esito |
|---|---|---|
| senza `CC` esportato | `i686-w64-mingw32-gcc` | ✅ |
| con `CC=/usr/bin/gcc` | `/usr/bin/gcc` | ❌ `C compiler cannot create executables` |

Quindi `gzip` risultava "fallito" **anche con gcc**, e ogni confronto che lo
includeva era falsato in partenza. Lo shim in `PATH` resta il meccanismo (D-11)
ed è sufficiente: `make` cerca `cc`, autoconf prova `gcc` poi `cc`, cmake
idem — tutti passano dallo shim. Un `--host=<triplet>` invece cerca
`<triplet>-gcc`, che nello shim **non** c'è, e trova il cross compilatore vero.
Il probe usa `LB_CC`, che è interno e non interferisce. `export_cc = true` per
chi lo rivuole.

### D-30 — Compilare come utente non privilegiato, come fa un buildd
`tar` non compilava: `configure: error: you should not run configure as root`.
Non è una toolchain che fallisce, è il mio harness che costruisce da root mentre
un buildd Debian costruisce da utente normale con `fakeroot` — tanto che
`-rfakeroot` è il default di `dpkg-buildpackage`.

* **Alternative** — (a) `FORCE_UNSAFE_CONFIGURE=1`; (b) creare un utente e
  costruire con `runuser` + `fakeroot`.
* **Presa: (b)**, con (a) come ripiego se `runuser`/`fakeroot` mancano.
* **Perché** — (a) zittisce *quel* controllo ma lascia tutto il resto diverso da
  una build reale: permessi, test suite, pacchetti che si comportano
  diversamente da root. Se il punto è "questo pacchetto si ricompila", allora va
  ricompilato nelle condizioni in cui lo si ricompila davvero.
* **Costo** — `runuser` azzera l'ambiente, quindi le variabili della toolchain
  vanno inoltrate esplicitamente (`_FORWARD` in `debian.py`). `PATH` è la più
  importante: porta lo shim.

### D-30b — Una variabile d'ambiente *vuota* non equivale a una non impostata
Corollario di D-29, trovato dalla matrice di esecuzione poche ore dopo averlo
introdotto. Attraversando `runuser` inoltravo le variabili con
`VAR="${VAR:-}"`. Da quando D-29 ha smesso di esportare `CC`, quella riga
materializzava `CC=""` **vuota** — che è peggio di non averla: una variabile
d'ambiente vuota batte comunque il default `cc` di make. La ricetta diventa
` -g -O2 ...`, `sh -c` si mangia il `-g` come propria opzione, e la build muore
con:

```
make[1]: g: No such file or directory
make[1]: O2: No such file or directory
```

un messaggio che non nomina né la causa né un file esistente. Ora si inoltrano
solo le variabili effettivamente valorizzate. `HOME` è stata tolta del tutto:
puntava a `/root`, illeggibile per l'utente di build.

**La lezione operativa**: il fix di D-29 era corretto e la sua verifica mirata
(gzip, tar) passava; a rompersi era un percorso *diverso*. Senza la matrice
completa sarebbe finito in un commit.

### D-31 — "La toolchain funziona qui" è una domanda separata da "l'archivio è raggiungibile"
`lazy-bootstrap toolchain check <target>` provisiona una toolchain ed esegue
compile+run, **senza** preparare la build machinery della distro.

* **Perché** — Fil-C e LLVM arrivano da GitHub; gcc e clang dall'archivio della
  distro. Dove l'archivio è irraggiungibile ma GitHub no, la prima domanda ha
  una risposta e la seconda no. Un unico comando che le mescola riporterebbe un
  limite di rete come toolchain rotta — l'errore che D-20 esiste per evitare.

### D-13 (correzione) — "pizfix = musl" riguarda il *bersaglio*, non l'eseguibile
Avevo scritto che su target musl si usa il build pizfix perché "self-contained".
Provandolo su `alpine` puro:

```
/opt/.../filc-0.681/build/bin/clang: not found      # ma il file c'è
readelf -l clang-20 -> /lib64/ld-linux-x86-64.so.2
readelf -d clang-20 -> libc.so.6, libm.so.6
```

Il **driver clang di Fil-C è linkato a glibc** anche nella variante pizfix: è il
*sysroot* a essere musl, non il compilatore. Su alpine puro non parte proprio.
La scelta della variante per libc (D-13) resta giusta per ciò che si *produce*;
quello che mancava è che il target deve comunque saper **eseguire** il
compilatore. Da qui la specifica originale `alpine + libc6-compat`: gcompat
serve prima ancora che ai pacchetti, al compilatore stesso.

Ora il fallimento è diagnosticato: si confrontano i loader presenti nel target
invece di fidarsi di `execve`, che per un interprete ELF mancante restituisce
ENOENT e quindi un "not found" che punta al file sbagliato.

**Rimedio verificato, non solo asserito.** Su una base musl che *ha* anche il
loader glibc (`frolvlad/alpine-glibc`: `/lib/ld-musl-x86_64.so.1` **e**
`/lib64/ld-linux-x86-64.so.2`) il clang di Fil-C parte e si identifica
regolarmente (`0.681 (pizfix, musl, clang 20.1.8)`). Il fallimento successivo è
un altro e molto più avanti — `unable to execute command: Executable "ld"
doesn't exist!` — cioè mancano i binutils, che richiedono `apk`. Quindi:
il loader glibc è **necessario e sufficiente per eseguire il compilatore**; per
compilare davvero serve anche il resto della toolchain dalla distro.

### D-32 — Nomi delle varianti e nomi degli asset non sono la stessa stringa
Costruivo l'URL come `{variante}-{versione}-linux-x86_64.tar.xz`, con variante
`pizfix` o `optfil`. Ma upstream pubblica il build musl come **`filc-*`**:

| asset | HTTP |
|---|---|
| `filc-0.681-linux-x86_64.tar.xz` | 200 |
| `optfil-0.681-linux-x86_64.tar.xz` | 200 |
| `pizfix-0.681-linux-x86_64.tar.xz` | **404** |

Ogni provisioning musl faceva 404. Ora la mappa variante→asset è esplicita
(`ASSET_PREFIX`) e c'è un test che la fissa. Vale come regola generale: quando
un identificatore interno finisce dentro un URL, va mappato, non concatenato.

### D-33 — Il proxy di uscita deve essere raggiungibile *dall'ambiente*, non solo dall'host
Sbloccare un dominio non basta se il traffico dell'ambiente non arriva al proxy.
Diagnosi completa, fatta misurando e non indovinando:

| dove | esito | causa |
|---|---|---|
| `curl` dall'host, con proxy | 200 | percorso sanzionato, funziona |
| `curl` dall'host, senza proxy | 403 | il filtro di rete è a monte |
| `apk` dentro un container | `Connection refused` | eredita `HTTPS_PROXY=http://127.0.0.1:PORT`, e **`127.0.0.1` nel container è il container** |
| `apk` dentro un chroot | `certificate not trusted` | il loopback è condiviso, ma la CA del proxy è ignota al rootfs |

Due ostacoli distinti, entrambi da superare:

1. **L'indirizzo.** Il container risolve `host.containers.internal` al gateway
   del bridge, ma il proxy ascolta **solo** su loopback: la porta lì è chiusa.
   Riscrivere l'indirizzo non basta — serve **condividere il network namespace
   dell'host** (`--network host`), che è ciò che rende utilizzabile il percorso
   sanzionato. Non è un aggiramento: il filtro a monte resta, e senza proxy la
   risposta è 403 comunque.
2. **La fiducia.** Un proxy che termina TLS presenta la propria CA. Nessun
   meccanismo singolo raggiunge tutti i client:

   | client | cosa serve davvero |
   |---|---|
   | `apk` (apk-tools 3) | **`SSL_CERT_FILE`** — *ignora* un certificato aggiunto in coda al bundle di sistema |
   | `apt` | il bundle di sistema, o `Acquire::https::CaInfo` |
   | `curl`, `git`, python | `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `REQUESTS_CA_BUNDLE` |

   Vengono impostati tutti. La differenza misurata su Alpine è fra
   `TLS: server certificate not trusted` e `28642 distinct packages available`.

**Nota implementativa** — la CA (187 KB) va **caricata come file**, non scritta
con `write_text`: quest'ultima mette il contenuto sulla riga di comando e
fallisce con `E2BIG` ("Argument list too long"), un errore che non nomina né la
CA né il proxy.

### D-13 (seconda correzione) — `libc6-compat` non basta per Fil-C
La specifica iniziale assumeva `alpine + libc6-compat` → variante musl di Fil-C.
Misurato: gcompat fornisce il **loader** ma non i **simboli**.

```
Error relocating clang-20: __wmemcpy_chk: symbol not found
Error relocating clang-20: __wmemset_chk: symbol not found
Error relocating clang-20: mallinfo2: symbol not found
```

Sono funzioni glibc (fortificate, e `mallinfo2`) che gcompat non implementa.
Quindi il driver clang di Fil-C **non gira su Alpine nemmeno con libc6-compat**:
serve una glibc vera. La diagnosi ora distingue i tre casi — loader assente,
loader presente ma simboli mancanti, binario che parte — perché portano a rimedi
completamente diversi.

### D-34 — Archivi grandi: streaming, non materializzazione (e stdin va chiesto)
Provisionare `llvm-20.1.8` ha esaurito il disco della macchina, e la causa non
era la dimensione della toolchain ma il percorso di scompattamento.

Quando il target non ha `xz` — cosa che accade ogni volta che il suo archivio
pacchetti è irraggiungibile, visto che `xz-utils` viene da lì — l'archivio
veniva decompresso in un `.tar` sull'host e quel tar veniva **copiato dentro**
l'ambiente:

| passo | costo |
|---|---|
| download `.tar.xz` | 1,9 GiB |
| decompressione sull'host | ~8 GiB |
| copia dentro l'ambiente | ~8 GiB |
| installazione finale | ~8 GiB |
| **totale** | **~25 GiB per installarne 8** |

`Executor` guadagna una primitiva, `feed`: esegue un produttore sull'host e ne
convoglia lo stdout dentro l'ambiente. Lo scompattamento di ripiego ora fa
`xz -dc | tar -x`, quindi **nulla di intermedio viene mai scritto**.

**Trabocchetto trovato subito dopo**: `podman exec` lascia lo stdin chiuso se
non gli si passa `-i`. L'archivio non arrivava a `tar`, che rispondeva
`This does not look like a tar archive` — un messaggio che accusa l'archivio
invece della pipe vuota che ha effettivamente letto. Da qui `_wrap_stdin`,
sovrascritto solo dal backend OCI.

**Esito**: `llvm-20.1.8` si provisiona (versione esatta, dal tarball upstream,
senza sostituzioni — D-12) su `debian:13`.

### D-35 — "Manca la libc" non è "la toolchain è rotta"
Il probe di `llvm-20.1.8` su `debian:13` fallisce così:

```
probe.c:2:10: fatal error: 'stdio.h' file not found
```

Un clang appena scompattato su un'immagine *runtime-only* non compila nulla
perché non ci sono intestazioni contro cui compilare, non perché sia rotto.
Riportarlo come toolchain fallita sarebbe lo stesso errore di categoria di
riportare un archivio bloccato come fallimento di build (D-20). Il probe ora
riconosce il caso e lo dice:

> the toolchain works, but this target has no C library headers
> (install libc6-dev / musl-dev / libc-dev). Nothing is wrong with the compiler.

---

## 5. Reporting

### D-14 — Un solo documento canonico (JSON), N renderer
* `RunReport` → JSON; testo, Markdown, HTML e confronto leggono **solo** quel JSON.
* **Perché** — Rende ri-renderizzabile un run vecchio, rende testabili i renderer senza
  eseguire build, e permette al gemello Bash di essere un cittadino di prima classe:
  gli basta emettere lo stesso JSON.

### D-15 — HTML autoconsistente, zero risorse esterne
* Un unico file, CSS e JS inline, nessuna CDN. È la "GUI" richiesta: si apre con un
  doppio click, si può allegare a una mail, funziona offline e in CI.

### D-16 — Il confronto è per **pacchetto × run**, con classificazione delle transizioni
* Categorie: `regression` (ok→fail), `fix` (fail→ok), `stable-ok`, `stable-fail`, `new`,
  `gone`. È la domanda vera quando si confrontano toolchain ("cosa si rompe con clang
  che con gcc funzionava"), non la semplice differenza di percentuali.

---

## 6. Debug e riproducibilità manuale

### D-17 — Ogni step è tracciabile e **rieseguibile a mano**
* Con `--debug` (o `LB_DEBUG=1`) ogni step emette: id stabile, backend, cwd, delta di
  environment, comando esatto, rc e durata.
* In più il run scrive `runs/<id>/replay/<unit>.sh`: uno script **realmente eseguibile**
  con la sequenza completa dei comandi lanciati nel target, commentata step per step.
* **Perché non solo `set -x`** — `set -x` mostra il comando *dentro* il target ma perde
  il wrapping del backend (`podman exec ... sh -c '...'`). Il replay li mostra entrambi:
  la riga che serve per riprodurre dal proprio terminale, e il corpo dello script.

### D-18 — Livelli di debug
* `LB_DEBUG=0` silenzioso · `1` step + rc + tempi · `2` anche corpo dello script e env
  · `3` anche output completo di ogni step (niente troncamento).

---

## 7. CI e immagini di build

### D-19 — `ci/` descrive **flavour** = distro × toolchain, non immagini a mano
* Un `Containerfile` per famiglia di distro, parametrizzato via `ARG`
  (`TOOLCHAIN_KIND`, `TOOLCHAIN_VERSION`, `PROVISION`), più `ci/flavours.toml` che
  elenca le combinazioni.
* **Perché** — Le combinazioni sono 2 distro × ~5 toolchain e cresceranno; scriverle a
  mano garantisce che divergano. Un `Containerfile` per famiglia + una tabella dati è
  la forma minima che le tiene allineate.
* **Granularità di build** — la stessa immagine serve i tre modi di raggruppamento
  (`all` / `group` / `package`): cambia quante volte la si istanzia, non cosa contiene.

---

## 8. Vincoli dell'ambiente di sviluppo corrente

### D-20 — Rete in *allowlist*: sorgenti Debian/Alpine irraggiungibili
Rilevato con `lazy-bootstrap doctor` (vedi `docs/ENVIRONMENT.md` per la mappa completa).

* Raggiungibili: `github.com` (+ release), `archive.ubuntu.com`, `security.ubuntu.com`,
  `pypi.org`, `registry.npmjs.org`, `mirror.gcr.io`.
* **Bloccati**: `deb.debian.org` e tutti i mirror Debian, `dl-cdn.alpinelinux.org` e
  tutti i mirror Alpine, `salsa.debian.org`, `sources.debian.org`,
  `git*.alpinelinux.org`, `ftp.gnu.org`, `kernel.org`,
  `production.cloudflare.docker.com` / `production.cloudfront.docker.com`
  (i *blob* di Docker Hub).
* **Conseguenze e scelte**
  1. **Registry mirror** (`--registry-mirror mirror.gcr.io`): riscrive `docker.io/...`
     e permette di scaricare davvero `debian:13-slim` e `alpine`. Adottato come
     opzione generale, non come workaround: serve a chiunque stia dietro a un proxy.
  2. **Source mirror** (`--source-mirror debian=<url>`): il driver apt punta a un
     archivio configurabile. In questo ambiente l'unico archivio apt raggiungibile è
     quello Ubuntu, quindi la validazione end-to-end della pipeline Debian gira contro
     `archive.ubuntu.com`; il risultato è marcato nel report (`environment.source_mirror`)
     per non spacciarlo per un rebuild di Debian.
  3. Lo stato `blocked` esiste apposta per distinguere "la rete non me lo lascia
     prendere" da "il pacchetto non compila". Confondere i due falserebbe il confronto
     fra toolchain, che è lo scopo del tool.
* **Non fatto di proposito** — nessun tentativo di aggirare l'allowlist (proxy di terzi,
  tunnel, mirror non ufficiali). È un controllo di sicurezza dell'ambiente: si segnala,
  non si scavalca. Gli host da aggiungere per un run completo sono elencati in
  `docs/ENVIRONMENT.md`.

### D-21 — I check di build in CI GitHub nascono disabilitati
Richiesto: la validazione la si fa in locale in questa iterazione. I workflow esistono,
sono completi e girano solo su `workflow_dispatch` o con la variabile di repo
`LAZY_BOOTSTRAP_CI=enabled`. Così il giorno in cui la CI serve, si accende una variabile
invece di scrivere i workflow di corsa.

---

## 9. Registro cronologico

| # | Data | Voce |
|---|---|---|
| 1 | 2026-07-31 | Impianto iniziale: D-01…D-21. |
| 2 | 2026-07-31 | Aggiunti gemello Bash (D-01), tracing/replay (D-17, D-18) su richiesta. |
| 3 | 2026-07-31 | D-22..D-24 emersi dalle prime build reali (flag LTO, shlibdeps, system-deps). |
| 4 | 2026-07-31 | D-25/D-26: matrice backend x rootfs, worker su qualsiasi classe. |
| 5 | 2026-07-31 | D-27: orchestrazione estratta come componente indipendente. |
| 6 | 2026-07-31 | D-28: classe VM (interfaccia generica + driver qemu). |
| 7 | 2026-07-31 | D-29..D-32 dai primi confronti reali: due bug che falsavano la baseline (CC esportato, build da root), l'asset musl di Fil-C, e la correzione a D-13. |

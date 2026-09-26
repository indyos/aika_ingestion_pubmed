"""``CorpusWriter``: confronto con il repository corpus, branch, commit e push.

Il connettore lavora su un clone locale del repository corpus (``corpus.local_path``, working
tree pulito). Lo stato esistente è letto da ``<remote>/<base_branch>`` con ``git show`` senza
toccare il working tree; ogni PR è un branch nuovo creato da lì, con un solo commit.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import CorpusConfig
from .document import ParsedDocument, parse_document
from .models import FrontMatter

type ChangeStatus = Literal["new", "updated"]

_PMID_FILE = re.compile(r"^pmid-([0-9]+)\.md$")


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Change:
    """Documento da proporre in PR."""

    front_matter: FrontMatter
    text: str  # file completo (front matter + corpo)
    status: ChangeStatus

    @property
    def pmid(self) -> str:
        return self.front_matter.id.removeprefix("pmid-")


class CorpusWriter:
    def __init__(self, config: CorpusConfig) -> None:
        self._cfg = config
        self._path = config.local_path
        self._base_ref = f"{config.remote}/{config.base_branch}"

    # ------------------------------------------------------------------ percorsi

    def relative_path(self, pmid: str) -> str:
        return f"{self._cfg.corpus_dir}/pmid-{pmid}.md"

    # ------------------------------------------------------------------ lettura

    def fetch(self, token: str | None = None) -> None:
        if not (self._path / ".git").exists():
            raise GitError(f"il clone del corpus non esiste: {self._path}")
        self._git("fetch", "--quiet", self._cfg.remote, self._cfg.base_branch, token=token)

    def existing_pmids(self) -> set[str]:
        return self._pmids_in(self._base_ref, self._cfg.corpus_dir)

    def rejected_pmids(self) -> set[str]:
        """PMID già rifiutati dal revisore (file in ``rejected_dir`` su ``<remote>/<base>``)."""
        return self._pmids_in(self._base_ref, self._cfg.rejected_dir)

    def _pmids_in(self, ref: str, directory: str) -> set[str]:
        out = self._git("ls-tree", "-r", "--name-only", ref, "--", directory)
        pmids: set[str] = set()
        for line in out.splitlines():
            m = _PMID_FILE.match(Path(line).name)
            if m:
                pmids.add(m.group(1))
        return pmids

    def read(self, pmid: str) -> ParsedDocument:
        text = self._git("show", f"{self._base_ref}:{self.relative_path(pmid)}")
        return parse_document(text)

    # ------------------------------------------------------------------ scrittura

    def remote_branches(self, prefix: str, token: str | None = None) -> set[str]:
        """Nomi dei branch sul remoto che iniziano con ``prefix`` (per non riusare un nome)."""
        out = self._git("ls-remote", "--heads", self._cfg.remote, f"{prefix}*", token=token)
        return {
            parts[1].removeprefix("refs/heads/")
            for line in out.splitlines()
            if len(parts := line.split()) == 2
        }

    def commit_branch(self, branch: str, files: Mapping[str, str], message: str) -> None:
        """Crea ``branch`` da ``<remote>/<base>`` con un solo commit dei ``files`` (percorso→testo)."""
        self._git("checkout", "--quiet", "-B", branch, self._base_ref)
        for rel, text in files.items():
            target = self._path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        self._git("add", "--", *files)
        self._git(
            "-c",
            f"user.name={self._cfg.git_user_name}",
            "-c",
            f"user.email={self._cfg.git_user_email}",
            "commit",
            "--quiet",
            "-m",
            message,
        )

    # ------------------------------------------------------------------ revisione

    def open_branch(self, branch: str, token: str | None = None) -> None:
        """Porta il working tree sul branch di una PR esistente (aggiornato dal remoto)."""
        self._git("fetch", "--quiet", self._cfg.remote, branch, token=token)
        self._git("checkout", "--quiet", "-B", branch, f"{self._cfg.remote}/{branch}")

    def branch_pmids(self, directory: str) -> set[str]:
        """PMID dei file in ``directory`` sul branch attualmente in checkout."""
        return self._pmids_in("HEAD", directory)

    def read_file(self, rel: str) -> str:
        return (self._path / rel).read_bytes().decode("utf-8")

    def commit_changes(self, files: Mapping[str, str], remove: Sequence[str], message: str) -> None:
        """Scrive ``files``, rimuove ``remove`` e crea un commit sul branch in checkout."""
        for rel, text in files.items():
            target = self._path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        if files:
            self._git("add", "--", *files)
        if remove:
            self._git("rm", "--quiet", "--", *remove)
        self._git(
            "-c",
            f"user.name={self._cfg.git_user_name}",
            "-c",
            f"user.email={self._cfg.git_user_email}",
            "commit",
            "--quiet",
            "-m",
            message,
        )

    def push(self, branch: str, token: str | None = None) -> None:
        self._git("push", "--quiet", self._cfg.remote, f"{branch}:{branch}", token=token)

    def back_to_base(self) -> None:
        self._git("checkout", "--quiet", "--detach", self._base_ref)

    # ------------------------------------------------------------------ git

    def _git(self, *args: str, token: str | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if token:
            # Header passato via ambiente: il token non compare in argv né in .git/config.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
            )
        proc = subprocess.run(
            ["git", "-C", str(self._path), "-c", "core.autocrlf=false", *args],
            capture_output=True,
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {args[0]} fallito: {stderr}")
        return proc.stdout.decode("utf-8")

"""Language identification and document-quality flags, benchmarked rather than assumed.

Why not just trust the entity? Because the entity is a *company*, not a language: CN01 and
CH01 may invoice in English/German/French, MA01 mixes French and Arabic, and a Spanish
entity can send an English proforma. So language is a property of the page, and it must be
measured against a ground truth that is independent enough to be useful.

Ground truth here is the entity's country (masters/entities.csv) for the unambiguous
countries, and a held-out split of documents. Three methods plus a fusion are compared:

* **stopwords** — fraction of tokens that are function words of each language (pure
  dictionary lookup, no training, ~microseconds);
* **char 3-gram centroids / kNN** — build one profile per language from the training half,
  then cosine similarity; kNN uses the k nearest documents instead of the centroid;
* **langdetect** — a ~1 MB ultra-light library (naive Bayes on character n-grams), the
  "small model" baseline;
* **fusion** — weighted vote of the three, with a confidence and an `unknown` fallback.

`quality()` is separate and deliberately dumb: it flags documents that are too short or
too non-textual to be worth parsing at all, because downstream every rule silently does the
wrong thing on a 60-character page.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# --------------------------------------------------------------------- corpus

STOPWORDS: dict[str, set[str]] = {
    "es": set("""de la que el en y a los se del las por un para con no una su al es lo como mas pero sus le ya o este
        si porque esta entre cuando muy sin sobre tambien me hasta hay donde quien desde todo nos durante todos uno
        les ni contra otros ese eso ante ellos esto mi antes algunos que unos yo otro otras otra tanto esa estos
        mucho quienes nada muchos cual poco ella estar estas algunas algo nosotros mis tu te ti tus ellas os mio
        nuestra nuestros vuestro suyo cuya cuyo ser son fue era han ha hemos estan esta estoy""".split()),
    "fr": set("""de la le et les des en un une du que est pour qui dans a par plus pas au sur ne se ce il elle nous vous
        ils elles avec son sa ses leur leurs mais ou donc car comme tout tous toute toutes cette ces cet aux etre
        avoir fait faire tres aussi bien deux sans sous entre encore meme si non oui apres avant chez lui moi toi
        mon ma mes ton ta tes notre votre nos vos y dont quel quelle quels quelles lors afin ainsi""".split()),
    "de": set("""der die das und in den von zu mit sich des auf für ist im dem nicht ein eine als auch es an werden aus
        er hat dass sie nach wird bei einer um am sind noch wie einem über einen so zum war haben nur oder aber vor
        zur bis mehr durch man sein wurde sei wir was wenn kann ich ihre ihrer alle vom dann unter soll ich ihre
        kann dieses dieser diese eines einem einer ohne sowie beim samt gegen etwa""".split()),
    "en": set("""the of and to in a is that for it as was with be by on not he i this are or his from at which but have
        an they you were her all she there would their we him been has when who will more no if out so said what up
        its about into than them can only other new some could time these two may then do first any my such please
        invoice total date due payment bank account""".split()),
    "pt": set("""de a o que e do da em um para com nao uma os no se na por mais as dos como mas foi ao ele das tem a seu
        sua ou ser quando muito ha nos ja esta eu tambem so pelo pela ate isso ela entre era depois sem mesmo aos ter
        seus quem nas me esse eles estao voce essa num nem suas meu minha numa pelos elas havia seja qual sera nos
        tenho lhe deles essas esses pelas este fosse dele tu te voces vos lhes meus minhas teu tua teus tuas nosso
        nossa nossos nossas dela delas esta estes estas aquele aquela""".split()),
    "it": set("""di che e la il un a per in una mi sono ho non con le si da lo ma come anche se questo quella al del
        della nel alla dei delle gli sul sulla tra piu o era essere stato stata hanno ha hai molto senza dopo dove
        quando quale quali cui suo sua loro noi voi io tu lui lei esso essa questi queste quello quelli quelle essere
        avere fare fatto gia ancora sempre gia""".split()),
    "nl": set("""de het een en van in is dat op te zijn met voor niet aan er die maar om ook als bij of uit dan nog
        door over tot je mij mijn wij ons onze hij zij ze hun haar hem was waren wordt worden heeft hebben had
        kunnen zou zullen dit deze dat die geen wel naar tussen tegen onder zonder tijdens omdat daar hier""".split()),
    "pl": set("""i w z na do nie to sie jest o a od za ze jak co po tak dla ale przez czy od oraz tym tego tej ten ta
        jego jej ich mi ty my wy on ona ono panstwo ktory ktora ktore nie ma byc sa bym bys by bylo byla byly przy
        nad pod bez oraz kiedy gdzie dlaczego jakiego bardzo moze""".split()),
}

#: country -> language, only where the mapping is unambiguous (used as ground truth)
COUNTRY_LANG = {
    "ES": "es", "MX": "es",
    "PT": "pt", "BR": "pt",
    "FR": "fr", "BE": "fr",
    "IT": "it",
    "DE": "de", "AT": "de",
    "NL": "nl",
    "GB": "en", "US": "en", "AU": "en", "IE": "en",
    "PL": "pl",
}

#: entities we keep out of the scored benchmark (multilingual by construction)
AMBIGUOUS_COUNTRIES = {"CH", "MA", "CN"}


def fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]{2,}", fold(text), re.UNICODE)


def ngrams(text: str, n: int = 3) -> Counter:
    """Character n-grams over letters and single spaces (language signal, layout-agnostic).

    `n=4` measured noticeably better than `n=3` on these short (~570 char) documents: a
    3-gram of a Romance language is shared by three languages, a 4-gram is not.
    """
    cleaned = re.sub(r"[^a-z ]+", " ", fold(text))
    cleaned = re.sub(r"\s+", " ", cleaned)
    return Counter(cleaned[i : i + n] for i in range(len(cleaned) - n + 1))


def trigrams(text: str) -> Counter:
    return ngrams(text, 3)


def _softmax(scores: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    """Confidences must be comparable across methods, so every score becomes a probability."""
    if not scores:
        return {}
    top = max(scores.values())
    exps = {k: math.exp((v - top) / temperature) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: v / total for k, v in exps.items()}


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# ------------------------------------------------------------------ methods


@dataclass
class StopwordDetector:
    """Dictionary method: the language whose function words appear most often."""

    name: str = "stopwords"
    weights: dict[str, float] = field(default_factory=dict)

    def fit(self, texts: list[str], labels: list[str]) -> StopwordDetector:
        # A word is only evidence if it is *characteristic*: subtract the best competing
        # language ("de" is Spanish and Dutch and French — it should count for nobody).
        counts = {lang: Counter() for lang in STOPWORDS}
        totals = Counter()
        for text, label in zip(texts, labels):
            for tok in _tokens(text):
                totals[label] += 1
                if tok in STOPWORDS[label]:
                    counts[label][tok] += 1
        shares = {
            lang: {tok: n / (totals[lang] or 1) for tok, n in counter.items()}
            for lang, counter in counts.items()
        }
        self.weights = {}
        for lang, own in shares.items():
            self.weights[lang] = {}
            for tok, share in own.items():
                rival = max((other.get(tok, 0.0) for other_lang, other in shares.items() if other_lang != lang), default=0.0)
                if share > rival:
                    self.weights[lang][tok] = share - rival
        return self

    def predict(self, text: str) -> tuple[str, float]:
        toks = _tokens(text)
        if not toks:
            return "unknown", 0.0
        scores = {}
        for lang, weights in self.weights.items():
            scores[lang] = sum(weights.get(tok, 0.0) for tok in toks) / len(toks)
        best = max(scores, key=scores.get)
        total = sum(scores.values()) or 1
        return best, round(scores[best] / total, 3)


@dataclass
class CentroidDetector:
    """Char 3-gram centroid per language; kNN variant uses the k nearest documents."""

    k: int = 0            # 0 = nearest centroid, >0 = k nearest training documents
    n: int = 4            # n-gram length
    temperature: float = 0.02  # softmax temperature, fitted on the validation split
    name: str = "centroid"
    centroids: dict[str, Counter] = field(default_factory=dict)
    samples: list[tuple[str, Counter]] = field(default_factory=list)

    def fit(self, texts: list[str], labels: list[str]) -> CentroidDetector:
        self.centroids = {}
        self.samples = []
        for text, label in zip(texts, labels):
            grams = ngrams(text, self.n)
            self.samples.append((label, grams))
            acc = self.centroids.setdefault(label, Counter())
            for gram, n in grams.items():
                acc[gram] += n
        self.name = f"knn{self.k}" if self.k else "centroid"
        return self

    def _top(self, text: str) -> Counter:
        """Only the 200 most frequent n-grams: kNN is O(train docs) per query, so the
        size of one document's vector is what decides whether the method is usable."""
        grams = ngrams(text, self.n)
        if len(grams) <= 200:
            return grams
        return Counter(dict(grams.most_common(200)))

    def predict(self, text: str) -> tuple[str, float]:
        grams = self._top(text)
        if not grams:
            return "unknown", 0.0
        if not self.k:
            scores = {lang: _cosine(grams, centroid) for lang, centroid in self.centroids.items()}
            probs = _softmax(scores, temperature=self.temperature)
            best = max(probs, key=probs.get)
            return best, round(probs[best], 3)
        votes: Counter = Counter()
        best_scores: dict[str, float] = {}
        for label, sample in self.samples:
            sim = _cosine(grams, sample)
            best_scores[label] = max(best_scores.get(label, 0.0), sim)
            votes[label] += sim
        # majority of the k strongest individual similarities
        top = sorted(((s, label) for s, label in ((_cosine(grams, s), l) for l, s in self.samples)), reverse=True)[: self.k]
        top_votes = Counter(label for _s, label in top)
        best = top_votes.most_common(1)[0][0]
        conf = top_votes[best] / self.k
        return best, round(conf, 3)


@dataclass
class LibraryDetector:
    """langdetect: naive Bayes on character n-grams, ~1 MB, the 'ultra light model'."""

    name: str = "langdetect"

    def fit(self, texts: list[str], labels: list[str]) -> LibraryDetector:  # noqa: ARG002
        return self

    def predict(self, text: str) -> tuple[str, float]:
        try:
            from langdetect import DetectorFactory, detect_langs
        except Exception:
            return "unknown", 0.0
        DetectorFactory.seed = 0  # deterministic
        if len(text.strip()) < 20:
            return "unknown", 0.0
        try:
            ranked = detect_langs(text)
        except Exception:
            return "unknown", 0.0
        if not ranked:
            return "unknown", 0.0
        top = ranked[0]
        return top.lang, round(float(top.prob), 3)


@dataclass
class SklearnKnnDetector:
    """kNN on sparse character n-grams, indexed by scikit-learn.

    Two details make this usable rather than academic:

    * vectors are hashed char 4-grams (`HashingVectorizer`, no vocabulary to fit) and
      L2-normalised, so **euclidean distance ranks exactly like cosine** — which is what
      lets ball_tree / kd_tree be used at all (they do not implement cosine);
    * `brute` with the cosine metric is kept as the reference: in high dimensions a tree
      usually degenerates to a linear scan, so it is worth measuring instead of assuming.

    kNN is O(log n) *only* when the metric is tree-friendly and the dimension is low; with
    16k hashed features the honest expectation is that brute-force sparse dot products win.
    """

    k: int = 5
    algorithm: str = "ball_tree"      # ball_tree | kd_tree | brute
    n_features: int = 1 << 14
    ngram: int = 4
    reduce_dims: int = 0              # >0 -> TruncatedSVD (dense), which is what makes trees work
    name: str = "knn_ball"
    _vec: object = None
    _svd: object = None
    _index: object = None
    _labels: object = None

    def fit(self, texts: list[str], labels: list[str]) -> SklearnKnnDetector:
        import numpy as np
        from sklearn.feature_extraction.text import HashingVectorizer
        from sklearn.neighbors import NearestNeighbors
        from sklearn.preprocessing import normalize

        self._vec = HashingVectorizer(
            analyzer="char_wb",
            ngram_range=(self.ngram, self.ngram),
            n_features=self.n_features,
            alternate_sign=False,
            norm=None,
            lowercase=True,
        )
        matrix = normalize(self._vec.transform(texts))
        metric = "cosine" if self.algorithm == "brute" else "euclidean"
        if self.reduce_dims:
            from sklearn.decomposition import TruncatedSVD

            # sparse trees are not supported by sklearn (it falls back to brute force), so
            # the only way to actually use a tree is to project the vectors to a low
            # dimension first. 64 components keeps most of the language signal.
            self._svd = TruncatedSVD(n_components=self.reduce_dims, random_state=0)
            matrix = normalize(self._svd.fit_transform(matrix))
        self._index = NearestNeighbors(n_neighbors=self.k, algorithm=self.algorithm, metric=metric)
        self._index.fit(matrix)
        self._labels = np.array(labels)
        self.name = f"knn_{self.algorithm}" + (f"_svd{self.reduce_dims}" if self.reduce_dims else "")
        return self

    def predict(self, text: str) -> tuple[str, float]:
        from collections import Counter as _Counter

        from sklearn.preprocessing import normalize

        if self._index is None or not text.strip():
            return "unknown", 0.0
        vector = self._vec.transform([text])
        if self._svd is not None:
            vector = self._svd.transform(vector)
        vector = normalize(vector)
        _dist, neighbours = self._index.kneighbors(vector, n_neighbors=self.k)
        votes = _Counter(self._labels[neighbours[0]])
        best, count = votes.most_common(1)[0]
        return str(best), round(count / self.k, 3)


@dataclass
class NaiveBayesDetector:
    """Multinomial naive Bayes on character 3-grams: ~1 ms to fit, microseconds to query.

    This is the "ultra light model" baseline that does not need a library: counts from the
    training half, add-one smoothing, log-likelihood per language.
    """

    alpha: float = 0.02   # smoothing dominates with 3-grams and a large vocabulary
    n: int = 4
    name: str = "naive_bayes"
    logp: dict[str, dict[str, float]] = field(default_factory=dict)
    prior: dict[str, float] = field(default_factory=dict)
    default: dict[str, float] = field(default_factory=dict)  # unseen-gram log prob
    vocab: set[str] = field(default_factory=set)

    def fit(self, texts: list[str], labels: list[str]) -> NaiveBayesDetector:
        counts: dict[str, Counter] = defaultdict(Counter)
        docs = Counter(labels)
        vocab: set[str] = set()
        for text, label in zip(texts, labels):
            grams = ngrams(text, self.n)
            counts[label].update(grams)
            vocab.update(grams)
        size = len(vocab) or 1
        total_docs = sum(docs.values()) or 1
        self.vocab = vocab
        self.prior = {lang: math.log(n / total_docs) for lang, n in docs.items()}
        self.logp = {}
        self.default = {}
        for lang, counter in counts.items():
            denom = sum(counter.values()) + self.alpha * size
            self.logp[lang] = {
                gram: math.log((counter.get(gram, 0) + self.alpha) / denom) for gram in vocab
            }
            # unseen grams must use the *language's* denominator, not the document's
            self.default[lang] = math.log(self.alpha / denom)
        return self

    def predict(self, text: str) -> tuple[str, float]:
        grams = ngrams(text, self.n)
        if not grams:
            return "unknown", 0.0
        total = sum(grams.values())
        scores = {}
        for lang, table in self.logp.items():
            scores[lang] = self.prior.get(lang, 0.0) + sum(
                n * table.get(gram, self.default[lang]) for gram, n in grams.items()
            ) / total
        probs = _softmax(scores, temperature=0.35)
        best = max(probs, key=probs.get)
        return best, round(probs[best], 3)


@dataclass
class GatedFusion:
    """Primary method first; consult the others only when the primary is not confident.

    Tuned the honest way: the gate threshold comes from the accuracy-by-margin curve on the
    training split, and the secondary voters are weighted by their own train accuracy.
    """

    primary: object
    secondary: list
    threshold: float = 0.0
    weights: list[float] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    name: str = "gated"

    def fit(self, texts: list[str], labels: list[str]) -> GatedFusion:
        self.primary.fit(texts, labels)
        for detector in self.secondary:
            detector.fit(texts, labels)
        self.labels = set(labels)
        if not self.weights:
            self.weights = [1.0] * len(self.secondary)
        return self

    def predict(self, text: str) -> tuple[str, float]:
        lang, conf = self.primary.predict(text)
        if conf >= self.threshold or lang not in self.labels:
            return lang, conf
        votes: dict[str, float] = {lang: conf}
        for detector, weight in zip(self.secondary, self.weights):
            other, other_conf = detector.predict(text)
            if other in self.labels:
                votes[other] = votes.get(other, 0.0) + weight * max(other_conf, 0.05)
        best = max(votes, key=votes.get)
        total = sum(votes.values()) or 1
        return best, round(votes[best] / total, 3)


@dataclass
class Fusion:
    """Weighted vote; the weights are the only thing tuned, and they are tuned on train."""

    detectors: list
    weights: list[float]
    labels: set[str]
    name: str = "fusion"

    def fit(self, texts: list[str], labels: list[str]) -> Fusion:
        for detector in self.detectors:
            detector.fit(texts, labels)
        self.labels = set(labels)
        return self

    def predict(self, text: str) -> tuple[str, float]:
        votes: dict[str, float] = {}
        for detector, weight in zip(self.detectors, self.weights):
            lang, conf = detector.predict(text)
            if lang in self.labels:  # do not let a library invent a language we cannot score
                votes[lang] = votes.get(lang, 0.0) + weight * max(conf, 0.05)
        if not votes:
            return "unknown", 0.0
        best = max(votes, key=votes.get)
        total = sum(votes.values()) or 1
        return best, round(votes[best] / total, 3)


# -------------------------------------------------------------------- flags


@dataclass(slots=True)
class Quality:
    chars: int
    letters: int
    digits: int
    flags: list[str]

    @property
    def ok(self) -> bool:
        return not self.flags


def quality(text: str, *, min_chars: int = 200, hard_min: int = 40) -> Quality:
    """Flag documents that are too thin to parse, before any rule runs on them.

    Thresholds are the ones the experiment measured: every readable PDF in the corpus has
    >= 300 characters; a page with < 200 is almost certainly a stub (a notice, a cover
    sheet) and < 40 means the text layer is missing entirely (scanned or image-only).
    """
    stripped = (text or "").strip()
    chars = len(stripped)
    letters = sum(ch.isalpha() for ch in stripped)
    digits = sum(ch.isdigit() for ch in stripped)
    flags: list[str] = []
    if chars < hard_min:
        flags.append("no_text")          # would need OCR
    elif chars < min_chars:
        flags.append("sparse")            # parses, but do not expect fields
    if chars and letters / chars < 0.35:
        flags.append("low_letters")       # tables of numbers, or extraction garbage
    if chars and digits / chars > 0.5:
        flags.append("mostly_digits")
    return Quality(chars=chars, letters=letters, digits=digits, flags=flags)


__all__ = [
    "AMBIGUOUS_COUNTRIES",
    "GatedFusion",
    "NaiveBayesDetector",
    "SklearnKnnDetector",
    "COUNTRY_LANG",
    "STOPWORDS",
    "CentroidDetector",
    "Fusion",
    "LibraryDetector",
    "Quality",
    "StopwordDetector",
    "fold",
    "ngrams",
    "quality",
    "trigrams",
]

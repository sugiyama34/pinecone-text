import json
import numpy as np
import tempfile
from pathlib import Path

import wget
from scipy import sparse
from typing import List, Callable, Optional, Dict, Union, Tuple
from sklearn.feature_extraction.text import HashingVectorizer

from pinecone_text.sparse import SparseVector
from pinecone_text.sparse.base_sparse_encoder import BaseSparseEncoder


class BM25(BaseSparseEncoder):
    """OKAPI BM25 implementation for single fit to a corpus
    (no continuous corpus updates supported)"""

    def __init__(
        self,
        tokenizer: Callable[[str], List[str]],
        vocabulary_size: int = 2**24,
        b: float = 0.75,
        k1: float = 1.2,
    ):
        """
        OKapi BM25 with HashingVectorizer

        Args:
            tokenizer: A function to converts text to a list of tokens
            vocabulary_size: The hash size to which the tokens are mapped to
            b: The length normalization parameter
            k1: The term frequency normalization parameter

        Example:

            ```python
            from pinecone_text.sparse import BM25

            bm25 = BM25(tokenizer=lambda x: x.split())

            bm25.fit([
                "The quick brown fox jumps over the lazy dog",
                "The lazy dog is brown",
                "The fox is brown"])

            bm25.encode_documents("The brown fox is quick")
            # {"indices": [102, 18, 12, ...], "values": [0.21, 0.38, 0.15, ...]}
            bm25.encode_queries("Which fox is brown?")
            # {"indices": [102, 16, 18, ...], "values": [0.21, 0.11, 0.15, ...]}
            ```
        """
        if vocabulary_size > 2**28 - 1:
            raise ValueError(
                "currently only supports vocabulary_size <= 2**28 - 1,"
                "upcoming versions will support vocabulary_size <= 2**32 - 1"
            )
        elif vocabulary_size <= 0:
            raise ValueError("vocabulary_size must be greater than 0")

        # Fixed params
        self.vocabulary_size: int = vocabulary_size
        self.b: float = b
        self.k1: float = k1

        self._tokenizer: Callable[[str], List[str]] = tokenizer

        self._tf_vectorizer = HashingVectorizer(
            n_features=self.vocabulary_size,
            token_pattern=None,
            tokenizer=self._tokenizer,
            norm=None,
            alternate_sign=False,
            binary=False,
        )

        # Learned Params
        self.doc_freq: Optional[Dict[int, float]] = None
        self.n_docs: Optional[int] = None
        self.avgdl: Optional[float] = None

    def fit(self, corpus: List[str]) -> "BM25":
        """
        Fit BM25 by calculating document frequency over the corpus

        Args:
            corpus: list of texts to fit BM25 with
        """
        tf_matrix = self._tf_vectorizer.transform(corpus)
        self.avgdl = tf_matrix.sum(axis=1).mean()
        self.n_docs = tf_matrix.shape[0]

        # make tf matrix binary (1 if term is present in doc, 0 otherwise)
        tf_matrix.data.fill(1)
        doc_tf_vector = sparse.csr_matrix(tf_matrix.sum(axis=0))
        self.doc_freq = {
            int(idx): float(val)
            for idx, val in zip(doc_tf_vector.indices, doc_tf_vector.data)
        }
        return self

    def encode_documents(
        self, texts: Union[str, List[str]]
    ) -> Union[SparseVector, List[SparseVector]]:
        """
        encode documents to a sparse vector (for upsert to pinecone)

        Args:
            texts: a single or list of documents to encode as a string
        """
        if self.doc_freq is None or self.n_docs is None or self.avgdl is None:
            raise ValueError("BM25 must be fit before encoding documents")

        if isinstance(texts, str):
            return self._encode_single_document(texts)
        elif isinstance(texts, list):
            return [self._encode_single_document(text) for text in texts]
        else:
            raise ValueError("texts must be a string or list of strings")

    def _encode_single_document(self, text: str) -> SparseVector:
        doc_tf = self._tf_vectorizer.transform([text])
        norm_doc_tf = self._norm_doc_tf(doc_tf)
        return {
            "indices": [int(x) for x in doc_tf.indices],
            "values": [float(x) for x in norm_doc_tf.tolist()],
        }

    def encode_queries(
        self, texts: Union[str, List[str]], mode: str = "normalized"
    ) -> Union[SparseVector, List[SparseVector]]:
        """
        encode query to a sparse vector

        Args:
            texts: a single or list of queries to encode as a string
        """
        if self.doc_freq is None or self.n_docs is None or self.avgdl is None:
            raise ValueError("BM25 must be fit before encoding queries")

        if mode not in ("normalized", "original"):
            raise ValueError("mode must be in {'normalized, 'original'}")

        if isinstance(texts, str):
            return self._encode_single_query(texts, mode=mode)
        elif isinstance(texts, list):
            return [self._encode_single_query(text, mode=mode) for text in texts]
        else:
            raise ValueError("texts must be a string or list of strings")

    def _encode_single_query(self, text: str, mode: str = "normalized") -> SparseVector:
        query_tf = self._tf_vectorizer.transform([text])
        indices, values = self._compose_query_tf(query_tf, mode=mode)
        return {
            "indices": [int(x) for x in indices],
            "values": [float(x) for x in values],
        }

    def dump(self, path: str) -> None:
        """
        Store BM25 params to a file in JSON format

        Args:
            path: full file path to save params in
        """
        with open(path, "w") as f:
            json.dump(self.get_params(), f)

    def load(self, path: str) -> "BM25":
        """
        Load BM25 params from a file in JSON format

        Args:
            path: full file path to load params from
        """
        with open(path, "r") as f:
            params = json.load(f)
        return self.set_params(**params)

    def get_params(
        self,
    ) -> Dict[str, Union[int, float, Dict[str, List[Union[int, float]]]]]:
        """Returns the BM25 params"""

        if self.doc_freq is None or self.n_docs is None or self.avgdl is None:
            raise ValueError("BM25 must be fit before storing params")

        tf_pairs = list(self.doc_freq.items())
        return {
            "avgdl": self.avgdl,
            "n_docs": self.n_docs,
            "doc_freq": {
                "indices": [int(idx) for idx, _ in tf_pairs],
                "values": [float(val) for _, val in tf_pairs],
            },
            "b": self.b,
            "k1": self.k1,
            "vocabulary_size": self.vocabulary_size,
        }

    def set_params(
        self,
        avgdl: float,
        n_docs: int,
        vocabulary_size: int,
        doc_freq: Dict[str, List[int]],
        b: float,
        k1: float,
    ) -> "BM25":
        """
        Set input parameters to BM25

        Args:
            avgdl: average document length in the corpus
            n_docs: number of documents in the corpus
            vocabulary_size: size of the vocabulary
            doc_freq: document frequency of each term in the corpus
            b: length normalization parameter
            k1: term frequency normalization parameter
        """
        self.avgdl = avgdl  # type: ignore
        self.n_docs = n_docs  # type: ignore
        self.doc_freq = {
            idx: val
            for idx, val in zip(doc_freq["indices"], doc_freq["values"])  # type: ignore
        }
        self.b = b  # type: ignore
        self.k1 = k1  # type: ignore
        self.vocabulary_size = vocabulary_size  # type: ignore
        return self

    def _compose_doc_tf(
        self, doc_tf: sparse.csr_matrix
    ) -> np.ndarray:  # renamed from `_norm_doc_tf()` since no longer normalization
        """Calculate BM25 normalized document term-frequencies"""
        b, k1, avgdl = self.b, self.k1, self.avgdl
        tf = doc_tf.data
        norm_tf = tf / (k1 * (1.0 - b + b * (tf.sum() / avgdl)) + tf)
        return norm_tf

    def _compose_query_tf(
        self, query_tf: sparse.csr_matrix, mode: str = "normalized"
    ) -> Tuple[
        np.ndarray, np.ndarray
    ]:  # renamed from `_norm_query_tf()` since no longer normalization
        """Calculate BM25 normalized query term-frequencies"""
        tf = np.array(
            [self.doc_freq.get(idx, 0) for idx in query_tf.indices]
        )  # type: ignore
        idf = np.log((self.n_docs + 1) / (tf + 0.5))  # type: ignore
        if mode == "normalized":
            return query_tf.indices, idf / idf.sum()
        elif mode == "original":
            return query_tf.indices, idf * (1 + self.k1)

    @staticmethod
    def default() -> "BM25":
        """Create a BM25 model from pre-made params for the MS MARCO passages corpus"""
        bm25 = BM25(lambda x: x.split())
        url = (
            "https://storage.googleapis.com/"
            "pinecone-datasets-dev/bm25_params/msmarco_bm25_params.json"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir, "msmarco_bm25_params.json")
            wget.download(url, str(tmp_path))
            bm25.load(str(tmp_path))
        return bm25

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import torch
from annoy import AnnoyIndex

from clip_index.utils import create_index
from clip_index.utils.config import QueryCfg

from .simple_tokenizer import tokenize


@dataclass
class AnnoyImage:
    query: str | None
    dist: float
    ref_id: int
    index_id: int | None = None
    image_id: int | None = None
    image_path: Path | None = None
    imagenet_classes: set[str] | None = None

    def add_image_data(self, imid: int, path: str):
        self.image_id = imid
        self.image_path = Path(path)
        assert self.image_path.exists()

    def __eq__(self, item) -> bool:
        same_image: bool = (
            self.image_id == item.image_id
            or self.image_path == item.image_path
            or (
                self.index_id == item.index_id
                and self.ref_id == item.ref_id
            )
        )
        same_query: bool = self.query == item.query
        return same_image and same_query


AnnoyQueries: TypeAlias = dict[str, list[AnnoyImage]]


def add_image_path(cur: sqlite3.Cursor, annoy_images: list[AnnoyImage]):
    """adds image path _inplace_."""
    for im in annoy_images:
        image_id = cur.execute(
            "SELECT image_id FROM annoy_index_image WHERE index_id = ? AND ref_id = ?",
            (im.index_id, im.ref_id),
        ).fetchone()
        if image_id is None:
            print("Image id not found annoy image:", im)
            continue
        else:
            image_id = image_id[0]
        image_path = cur.execute(
            "SELECT image_path FROM image WHERE image_id = ?", (image_id,)
        ).fetchone()[0]
        im.add_image_data(image_id, image_path)


def create_query_embeddings(model, queries: list[str]) -> torch.Tensor:
    token_ids = tokenize(queries)
    encoded_text = model(token_ids)
    return encoded_text


def query_index(
    index_folder: Path,
    queries: list[str],
    cur: sqlite3.Cursor | None = None,
    cfg: QueryCfg = QueryCfg(),
) -> AnnoyQueries:
    encoded_text = create_query_embeddings(cfg.load_model(), queries)
    assert index_folder.exists()
    index = create_index()
    index_paths = [
        str(index_folder / file)
        for file in os.listdir(index_folder)
        if file.endswith(".ann")
    ]
    ann_queries: AnnoyQueries = {q: [] for q in queries}
    for path in index_paths:
        index_id = int(Path(path).stem)
        index.load(path)
        for q, q_emb in zip(queries, encoded_text):
            index_ref_ids, distances = index.get_nns_by_vector(
                q_emb,  # pyright: reportGeneralTypeIssues=false
                cfg.max_results_per_query,
                include_distances=True,
                search_k=cfg.search_k,
            )
            for ref_id, dist in zip(index_ref_ids, distances):
                if dist > cfg.threshold:
                    continue
                ann_img = AnnoyImage(
                    query=q, ref_id=ref_id, index_id=index_id, dist=dist
                )
                ann_queries[q].append(ann_img)

        if cur is not None:
            for ann_imgs in ann_queries.values():
                add_image_path(cur, ann_imgs)
        index.unload()
    return ann_queries


def filter_closest_n_results(query_image_dict, n_results):
    distances = sorted([j.dist for i in query_image_dict.values() for j in i])
    if len(distances) > n_results - 1:
        thres_dist = distances[n_results - 1]
        for q, imgs in query_image_dict.items():
            filterd_imgs = list(filter(lambda x: x.dist <= thres_dist, imgs))
            query_image_dict[q] = filterd_imgs


def create_item_comp_dict(
    index: AnnoyIndex, search_k: int = -1
) -> dict[int, AnnoyImage]:
    """
    Create a dictionary of all items in index to distances of all other items.
    """
    item_dict = {}
    for item_id in range(index.get_n_items()):
        res_ids = index.get_nns_by_item(
            item_id, index.get_n_items(), include_distances=True, search_k=search_k
        )
        item_dict[item_id] = [
            AnnoyImage(query=None, ref_id=ind_id, dist=dist)
            for ind_id, dist in zip(*res_ids)
        ]
    return item_dict

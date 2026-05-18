from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredDocxAsset:
    absolute_path: str
    relative_path: str
    name: str
    extension: str


class DocxAssetStore:
    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        self.image_dir = os.path.join(output_dir, "images")
        self.table_dir = os.path.join(output_dir, "tables")

    def reset(self) -> None:
        self._reset_dir(self.table_dir)
        self._reset_dir(self.image_dir)

    def write_image(self, name: str, extension: str, data: bytes) -> StoredDocxAsset:
        absolute_path = os.path.join(self.image_dir, f"{name}{extension}")
        with open(absolute_path, "wb") as image_file:
            image_file.write(data)
        return StoredDocxAsset(
            absolute_path=absolute_path,
            relative_path=f"images/{name}{extension}",
            name=name,
            extension=extension,
        )

    def rename_image(self, asset: StoredDocxAsset, new_name: str) -> StoredDocxAsset:
        new_absolute_path = os.path.join(self.image_dir, f"{new_name}{asset.extension}")
        if asset.absolute_path != new_absolute_path:
            os.rename(asset.absolute_path, new_absolute_path)
        return StoredDocxAsset(
            absolute_path=new_absolute_path,
            relative_path=f"images/{new_name}{asset.extension}",
            name=new_name,
            extension=asset.extension,
        )

    def write_table(self, name: str, html: str) -> StoredDocxAsset:
        absolute_path = os.path.join(self.table_dir, f"{name}.html")
        with open(absolute_path, "w", encoding="utf-8") as table_file:
            table_file.write(html)
        return StoredDocxAsset(
            absolute_path=absolute_path,
            relative_path=f"tables/{name}.html",
            name=name,
            extension=".html",
        )

    @staticmethod
    def _reset_dir(directory: str) -> None:
        if os.path.isdir(directory):
            shutil.rmtree(directory)
        os.makedirs(directory, exist_ok=True)

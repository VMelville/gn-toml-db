# Blender Geometry Nodes IR Add-on

このアドオンは、Blender 内の Text データブロックに記述された **TOML 形式の中間表現 (IR)** を読み取り、  
対応する **Geometry Nodes ノードグループとオブジェクト** を自動生成します。

## 機能概要

- Blender 上の任意の Text（TOML）を選び、Geometry Nodes を生成
- ノード作成、ソケット接続、Group Input/Output の構築を IR に基づき自動化
- 同じ IR を何度実行しても安全なように、ノードツリーやオブジェクトを適切に再構築
- Add-on メニューから操作可能

---

## インストール方法

1. 本アドオンを ZIP 化する  
   （`__init__.py` を含むフォルダを zip 圧縮）

2. Blender で  
   **編集 > プリファレンス > アドオン > インストール…**  
   から ZIP を選択してインストール

3. 「Geometry Nodes IR Builder」を有効化

---

## 使い方

### 1. IR（TOML）を書く

Blender の「テキストエディター」に以下のような TOML を記述します。

```toml
[info]
name = "MyObject"

[[node]]
id = "cube"
type = "GeometryNodeMeshCube"

[node.inputs.Size]
value = [1.0, 1.0, 1.0]

[output.Geometry]
from = "cube.Mesh"
```

### 2. Add-on パネルからビルド

3D ビューの右側（N パネル）に新しく追加される

**Geometry Nodes IR**

というパネルを開きます。

- 「IR テキストを選択」 で Text ブロック名を選択
- 「Build Geometry Nodes」ボタンを押すと  
  → Geometry Nodes ノードグループとオブジェクトが生成されます

---

## TOML IR の構造

### [info]

| キー | 説明 |
|------|------|
| name | 生成するノードグループおよびオブジェクト名 |

---

### [[parameter]]

Group Input に追加されるパラメーター定義です。

```toml
[[parameter]]
name = "Bottle height"
socket_type = "NodeSocketFloat"
default_value = 0.22
```

---

### [[node]]

ノードを作成し、ID で管理します。

```toml
[[node]]
id   = "resample"
type = "GeometryNodeResampleCurve"
```

---

### [node.inputs.<ソケット名>]

ノードの入力ソケットへ

- 定数値（value）
- 別ノードの出力（from）

を接続できます。

```toml
[node.inputs.Count]
value = 64

[node.inputs.Curve]
from = "curve_line.Curve"
```

複数入力が可能なソケット（Join Geometry など）は配列で記述します。

```toml
[[node.inputs.Geometry]]
from = "a.Mesh"

[[node.inputs.Geometry]]
from = "b.Mesh"
```

---

### [output]

Group Output へ接続する終端ノードを指定します。

```toml
[output.Geometry]
from = "join.Geometry"
```

---

## 冪等性（再実行しても壊れない設計）

このアドオンは複数回実行しても正しく動作するよう設計されています。

- 既存のノードツリーはクリアしたうえで安全に再構築
- 既存オブジェクトがある場合は再利用し、Geometry Nodes モディファイアのみ更新
- Text が変更されても毎回同じ結果が生成される

---

## 注意点

- Blender のノードソケット名は UI 表示名と内部名が異なることがあります  
  → IR には「内部ソケット名」を記述してください
- 「ShaderNodeValue」の default_value は outputs.Value に反映されるため、  
  Add-on 側で特別処理しています

---

## ライセンス

本アドオンは自由に改変・再利用できます。


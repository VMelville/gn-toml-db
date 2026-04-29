# Blender Geometry Nodes IR Add-on

このプロジェクトは、指定フォルダ内の **YAML 形式の中間表現 (IR)** ファイルを読み取り、  
対応する **Geometry Nodes ノードグループ / オブジェクト / Material** を生成するアドオンです。

TOML は配列やネストが増えると記述が冗長になりやすいため、このリポジトリでは **TOML を廃止し、YAML に完全移行** しています。

## 機能概要

- 指定フォルダ内の YAML ファイルをドロップダウン選択して Geometry Nodes を生成
- ノード作成、ソケット接続、Group Input/Output 構築を IR から自動化
- Material ノードツリーの構築と割り当てにも対応
- 同じ IR を再実行しても壊れにくいよう、ノードツリーやオブジェクトを再構築
- `PyYAML` には依存せず、単一ファイル内蔵の軽量 YAML パーサで動作

## ファイル構成

- `gn_from_yaml_ir.py`
  Add-on 本体と YAML パーサをまとめた単一ファイル
- `yamls/*.yaml`
  サンプル IR

## インストール

1. `gn_from_yaml_ir.py` をそのまま使うか、必要なら単体で配布します。
2. Blender で **編集 > プリファレンス > アドオン > インストール…** を開きます。
3. `gn_from_yaml_ir.py` を選択してインストールし、アドオンを有効化します。

## 使い方

### 1. YAML IR ファイルを用意する

```yaml
info:
  name: "MyObject"

node:
  - id: "cube"
    type: "GeometryNodeMeshCube"
    inputs:
      Size:
        value: [1.0, 1.0, 1.0]

output:
  Geometry:
    from: "cube.Mesh"
```

### 2. Add-on パネルから Build する

3D ビュー右側の N パネルに追加される **YAML IR** タブを開きます。

- `Folder` に YAML ファイルを置いたディレクトリを指定
- `YAML` ドロップダウンから対象ファイルを選択
- `Build` を押す

すると Geometry Nodes ノードグループとオブジェクトが生成されます。

Blender の相対パスも使えるので、たとえば `.blend` ファイル基準で `//yamls/` のような指定もできます。

## YAML IR の構造

### `info`

生成物の名前など、IR 全体のメタ情報です。

```yaml
info:
  name: "MyObject"
```

### `parameter`

Group Input に追加するパラメータ定義です。

```yaml
parameter:
  - name: "Bottle height"
    socket_type: "NodeSocketFloat"
    default_value: 0.22
```

### `node`

ノード定義の配列です。各要素は `id` と `type` を持ちます。

```yaml
node:
  - id: "resample"
    type: "GeometryNodeResampleCurve"
```

### `inputs`

入力ソケットには次のいずれかを指定できます。

- `from`: 他ノード出力とのリンク
- `value`: 定数値
- `material`: Material データブロック名

```yaml
node:
  - id: "cube"
    type: "GeometryNodeMeshCube"
    inputs:
      Size:
        value: [1.0, 1.0, 1.0]
```

複数入力ソケットは配列で書けます。

```yaml
node:
  - id: "join"
    type: "GeometryNodeJoinGeometry"
    inputs:
      Geometry:
        - from: "a.Mesh"
        - from: "b.Mesh"
```

### `outputs`

Value ノードなど、出力ソケットの `default_value` を設定したいときに使います。

```yaml
node:
  - id: "cap_radius_const"
    type: "ShaderNodeValue"
    outputs:
      Value:
        value: 0.015
```

### `repeat_items` / `zone_pair`

Repeat Zone のような動的ソケットを持つノードでは、追加ソケット定義とペア情報を
ノード定義に書けます。

```yaml
node:
  - id: "repeat_in"
    type: "GeometryNodeRepeatInput"
    zone_pair: "repeat_out"
    inputs:
      Iterations:
        from: "input.Count"
      Geometry:
        from: "frame.Geometry"

  - id: "repeat_out"
    type: "GeometryNodeRepeatOutput"
    repeat_items:
      - socket_type: "FLOAT"
        name: "Offset Z"
```

- `zone_pair`: `pair_with_output(...)` が必要なゾーン入力ノード用
- `repeat_items`: `GeometryNodeRepeatOutput.repeat_items.new(socket_type, name)` 相当
- `Iterations` は `Repeat Input` 側に与えます

### `output`

Group Output への接続先です。

```yaml
output:
  Geometry:
    from: "join.Geometry"
```

### `material`

Material ノードツリーを IR で構築したい場合に指定します。

```yaml
material:
  name: "MyMaterial"
  node:
    - id: "bsdf"
      type: "ShaderNodeBsdfPrincipled"
  output:
    Surface:
      from: "bsdf.BSDF"
```

## YAML パーサについて

Blender 同梱 Python では `PyYAML` が使えない環境があるため、`gn_from_yaml_ir.py` の中に小さな YAML パーサを内蔵しています。  
このパーサは次の記法を対象にしています。

- インデントベースのマッピング
- シーケンス
- インライン配列
- 文字列、数値、真偽値、`null`
- `|` による複数行文字列

このプロジェクトの IR を扱うには十分ですが、汎用 YAML 実装ではありません。

## サンプル

`yamls/` 配下に、以下のサンプル IR を置いています。

- `Chair.yaml`
- `CoffeeCap.yaml`
- `Eraser.yaml`
- `Pencil.yaml`
- `PetBottle.yaml`
- `RepeatZoneProbe.yaml`
- `Table.yaml`

## 冪等性

複数回実行しても結果が安定するよう、次の挙動にしています。

- 既存ノードツリーはクリアして再構築
- 既存オブジェクトがあれば再利用
- Geometry Nodes モディファイアは同名のものを再利用
- Material も同名で再利用

## 注意点

- ソケット名は Blender の内部名を使ってください
- YAML パーサはこのプロジェクト用の軽量実装です
- 配布や持ち運びは `gn_from_yaml_ir.py` 単体を前提にしています
- フォルダ内の列挙対象は `.yaml` / `.yml` ファイルです

## ライセンス

自由に改変・再利用できます。

bl_info = {
    "name": "Geometry & Material from TOML IR",
    "author": "ChatGPT + User",
    "version": (0, 4, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > TOML IR",
    "description": "Build Geometry Nodes and Material from TOML IR in a Text datablock",
    "category": "Node",
}

import bpy
import tomllib
from bpy.types import Operator, Panel
from bpy.props import EnumProperty


# ============================================================
# IR Loader from Blender Text block
# ============================================================

def load_ir_from_text(text_name: str) -> dict:
    """
    Blender の Text データブロックから IR TOML を読み込む。
    text_name: bpy.data.texts に存在するテキストの名前
    """
    if text_name not in bpy.data.texts:
        raise ValueError(f"Text '{text_name}' not found in bpy.data.texts")

    txt = bpy.data.texts[text_name]
    raw = txt.as_string()
    return tomllib.loads(raw)


# ============================================================
# Geometry Nodes Builder
# ============================================================

def build_geometry_nodes_from_ir(ir: dict):
    info = ir["info"]
    gn_name = info["name"]

    # --------------------------------------------------------
    # NodeGroup
    # --------------------------------------------------------
    node_group = bpy.data.node_groups.get(gn_name)
    if node_group is None:
        node_group = bpy.data.node_groups.new(gn_name, "GeometryNodeTree")

    # --------------------------------------------------------
    # Interface
    # --------------------------------------------------------
    iface = node_group.interface
    for item in list(iface.items_tree):
        iface.remove(item)

    # Group Input sockets
    params = ir.get("parameter", [])
    for p in params:
        s = iface.new_socket(
            name=p["name"],
            in_out="INPUT",
            socket_type=p["socket_type"],
            description=p.get("description", "")
        )
        if "subtype" in p:
            s.subtype = p["subtype"]
        if "default_value" in p:
            s.default_value = p["default_value"]
        if isinstance(p.get("min_value"), (int, float)):
            s.min_value = p["min_value"]
        if isinstance(p.get("max_value"), (int, float)):
            s.max_value = p["max_value"]

    # Group Output: Geometry
    iface.new_socket(
        name="Geometry",
        in_out="OUTPUT",
        socket_type="NodeSocketGeometry"
    )

    # --------------------------------------------------------
    # Node tree
    # --------------------------------------------------------
    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    n_in = nodes.new("NodeGroupInput")
    n_in.location = (-2200, 0)

    n_out = nodes.new("NodeGroupOutput")
    n_out.location = (900, 0)

    # --------------------------------------------------------
    # Node creation
    # --------------------------------------------------------
    node_map = {}

    for nd in ir.get("node", []):
        nid = nd["id"]
        ntype = nd["type"]
        node = nodes.new(ntype)
        node.name = nid
        node_map[nid] = node

        # props 設定
        # GeometryNodeSetMaterial の props.material は後方互換としてサポート
        for k, v in nd.get("props", {}).items():
            if node.bl_idname == "GeometryNodeSetMaterial" and k == "material" and isinstance(v, str):
                mat = bpy.data.materials.get(v)
                if mat is None:
                    print(f"[TOML-IR] WARNING: material '{v}' not found for SetMaterial node '{nid}' (props)")
                else:
                    node.material = mat
            else:
                setattr(node, k, v)

    # --------------------------------------------------------
    # Socket resolver
    # --------------------------------------------------------
    def resolve_output_socket(endpoint: str):
        owner, sock = endpoint.split(".", 1)
        if owner == "input":
            return n_in.outputs[sock]
        elif owner == "output":
            return n_out.outputs[sock]
        else:
            return node_map[owner].outputs[sock]

    # --------------------------------------------------------
    # Process inputs
    #  - "from"      : ソケットリンク
    #  - "value"     : default_value 代入
    #  - "material"  : Material データブロックを Material ソケットに設定
    # --------------------------------------------------------
    for nd in ir.get("node", []):
        node = node_map[nd["id"]]
        inputs_spec = nd.get("inputs", {})

        for socket_name, spec in inputs_spec.items():
            in_sock = node.inputs[socket_name]

            # Multi-input case (Join Geometry など)
            if isinstance(spec, list):
                for item in spec:
                    if "from" in item:
                        src = resolve_output_socket(item["from"])
                        links.new(src, in_sock)
                    elif "material" in item and isinstance(item["material"], str):
                        mat = bpy.data.materials.get(item["material"])
                        if mat is None:
                            print(f"[TOML-IR] WARNING: material '{item['material']}' not found for node '{nd['id']}', socket '{socket_name}' (multi)")
                        else:
                            # Material ソケットの default_value に Material を直接入れる
                            in_sock.default_value = mat
                    elif "value" in item:
                        in_sock.default_value = item["value"]
                continue

            # Single input
            if "from" in spec:
                src = resolve_output_socket(spec["from"])
                links.new(src, in_sock)
            elif "material" in spec and isinstance(spec["material"], str):
                mat = bpy.data.materials.get(spec["material"])
                if mat is None:
                    print(f"[TOML-IR] WARNING: material '{spec['material']}' not found for node '{nd['id']}', socket '{socket_name}'")
                else:
                    in_sock.default_value = mat
            elif "value" in spec:
                in_sock.default_value = spec["value"]

    # --------------------------------------------------------
    # Process outputs (default_value for Value nodes, etc.)
    #  - ShaderNodeValue などの出力 default_value を TOML 側から設定可能にする
    # --------------------------------------------------------
    for nd in ir.get("node", []):
        node = node_map[nd["id"]]
        outputs_spec = nd.get("outputs", {})

        for socket_name, spec in outputs_spec.items():
            out_sock = node.outputs[socket_name]

            if isinstance(spec, list):
                for item in spec:
                    if "value" in item:
                        out_sock.default_value = item["value"]
                continue

            if "value" in spec:
                out_sock.default_value = spec["value"]

    # --------------------------------------------------------
    # Group Output
    # --------------------------------------------------------
    output_spec = ir.get("output", {})
    for socket_name, spec in output_spec.items():
        src = resolve_output_socket(spec["from"])
        dst = n_out.inputs[socket_name]
        links.new(src, dst)

    # --------------------------------------------------------
    # Object + Modifier（再実行しても安定）
    # --------------------------------------------------------
    obj = bpy.data.objects.get(gn_name)
    if obj is None:
        mesh = bpy.data.meshes.new(gn_name + "Mesh")
        obj = bpy.data.objects.new(gn_name, mesh)
        bpy.context.collection.objects.link(obj)

    bpy.context.view_layer.objects.active = obj

    mod = None
    for m in obj.modifiers:
        if m.type == "NODES" and m.name == gn_name:
            mod = m
            break
    if mod is None:
        mod = obj.modifiers.new(name=gn_name, type="NODES")

    mod.node_group = node_group

    return node_group, obj


# ============================================================
# Material Builder
# ============================================================

def build_material_from_ir(ir: dict):
    """
    IR 内に [material] セクションがあれば、マテリアルノードツリーを構築して返す。
    オブジェクトへの割り当ては build_from_text 側で実施する。
    """
    mat_spec = ir.get("material")
    if not mat_spec:
        return None

    info = ir.get("info", {})
    mat_name = mat_spec.get("name") or (info.get("name", "TOML_Mat") + "_Mat")

    # Material の作成/取得
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    # Material Output ノード
    mat_out = nodes.new("ShaderNodeOutputMaterial")
    mat_out.name = "MaterialOutput"
    mat_out.location = (400, 0)

    node_map = {"MaterialOutput": mat_out}

    # --------------------------------------------------------
    # ノード生成
    # --------------------------------------------------------
    for nd in mat_spec.get("node", []):
        nid = nd["id"]
        ntype = nd["type"]
        node = nodes.new(ntype)
        node.name = nid
        node_map[nid] = node

        for k, v in nd.get("props", {}).items():
            setattr(node, k, v)

    # --------------------------------------------------------
    # ソケット解決
    # --------------------------------------------------------
    def resolve_output_socket(endpoint: str):
        owner, sock = endpoint.split(".", 1)
        return node_map[owner].outputs[sock]

    # --------------------------------------------------------
    # 入力処理
    # --------------------------------------------------------
    for nd in mat_spec.get("node", []):
        node = node_map[nd["id"]]
        inputs_spec = nd.get("inputs", {})

        for socket_name, spec in inputs_spec.items():
            in_sock = node.inputs[socket_name]

            if isinstance(spec, list):
                for item in spec:
                    if "from" in item:
                        src = resolve_output_socket(item["from"])
                        links.new(src, in_sock)
                    elif "value" in item:
                        in_sock.default_value = item["value"]
                continue

            if "from" in spec:
                src = resolve_output_socket(spec["from"])
                links.new(src, in_sock)
            elif "value" in spec:
                in_sock.default_value = spec["value"]

    # --------------------------------------------------------
    # 出力ソケットの default_value 設定（Value ノード等）
    # --------------------------------------------------------
    for nd in mat_spec.get("node", []):
        node = node_map[nd["id"]]
        outputs_spec = nd.get("outputs", {})

        for socket_name, spec in outputs_spec.items():
            out_sock = node.outputs[socket_name]

            if isinstance(spec, list):
                for item in spec:
                    if "value" in item:
                        out_sock.default_value = item["value"]
                continue

            if "value" in spec:
                out_sock.default_value = spec["value"]

    # --------------------------------------------------------
    # Material Output への接続
    # --------------------------------------------------------
    out_spec = mat_spec.get("output", {})
    for socket_name, spec in out_spec.items():
        if socket_name not in mat_out.inputs:
            continue
        src = resolve_output_socket(spec["from"])
        dst = mat_out.inputs[socket_name]
        links.new(src, dst)

    return mat


# ============================================================
# Shortcut helper
# ============================================================

def build_from_text(text_name: str):
    """Blender の Text 名を指定して Geometry & Material を構築."""
    ir = load_ir_from_text(text_name)

    # 1. 先にマテリアルを構築（SetMaterial ノードから参照される前に）
    mat = build_material_from_ir(ir)

    # 2. Geometry Nodes を構築
    node_group, obj = build_geometry_nodes_from_ir(ir)

    # 3. オブジェクトにマテリアルを割り当て（とりあえずスロット 0）
    if mat is not None and obj is not None and hasattr(obj.data, "materials"):
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)

    # Eevee をレンダラーに設定
    try:
        bpy.context.scene.render.engine = 'BLENDER_EEVEE'
    except Exception:
        pass

    return node_group, mat, obj


# ============================================================
# UI / Operator
# ============================================================

def text_enum_items(self, context):
    items = []
    for txt in bpy.data.texts:
        items.append((txt.name, txt.name, ""))
    if not items:
        items.append(("NONE", "NONE", "No Text"))
    return items


class OBJECT_OT_build_from_toml(Operator):
    bl_idname = "object.build_from_toml_ir"
    bl_label = "Build Geometry & Material from TOML IR"
    bl_description = "選択された Text を TOML IR として解釈し、Geometry Nodes と Material を生成"
    bl_options = {'REGISTER', 'UNDO'}

    # Operator 自身にも EnumProperty を持たせる（Panel から値を渡す）
    text_name: EnumProperty(
        name="Text",
        description="TOML IR を含む Text datablock",
        items=text_enum_items,
    )

    def execute(self, context):
        if self.text_name == "NONE":
            self.report({'ERROR'}, "No Text datablock selected")
            return {'CANCELLED'}

        try:
            node_group, mat, obj = build_from_text(self.text_name)
        except Exception as e:
            self.report({'ERROR'}, f"Failed: {e}")
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            f"Built NodeGroup '{node_group.name}' and Material '{mat.name if mat else 'None'}'"
        )
        return {'FINISHED'}


class VIEW3D_PT_toml_ir_builder(Panel):
    bl_label = "TOML IR Builder"
    bl_idname = "VIEW3D_PT_toml_ir_builder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TOML IR'

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="Build from TOML Text:")

        # Scene 側の EnumProperty から Text を選択
        col.prop(context.scene, "toml_ir_text_name", text="Text")

        # Operator を呼び出しつつ、選択された Text 名を渡す
        op = col.operator(OBJECT_OT_build_from_toml.bl_idname, text="Build")
        op.text_name = context.scene.toml_ir_text_name


def register():
    bpy.utils.register_class(OBJECT_OT_build_from_toml)
    bpy.utils.register_class(VIEW3D_PT_toml_ir_builder)

    # シーン側に EnumProperty を定義して、Text データブロックをプルダウン選択
    bpy.types.Scene.toml_ir_text_name = EnumProperty(
        name="TOML Text",
        description="TOML IR を含む Text datablock",
        items=text_enum_items,
    )


def unregister():
    del bpy.types.Scene.toml_ir_text_name
    bpy.utils.unregister_class(VIEW3D_PT_toml_ir_builder)
    bpy.utils.unregister_class(OBJECT_OT_build_from_toml)


if __name__ == "__main__":
    register()

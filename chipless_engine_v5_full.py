#!/usr/bin/env python3
"""
chipless_engine_v5_full.py
Chipless Quantum Engine v5 — Full (molecules + proteins/DNA support, full Bloch vectors,
entanglement heatmaps, similarity measures, optional AutoDock Vina wrapper).

Run:
    python chipless_engine_v5_full.py
Open in browser: http://127.0.0.1:8050

Notes:
- Uses RDKit if available for molecule parsing and preview.
- Uses BioPython (optional) for FASTA parsing; falls back to simple parser.
- Optional docking with `vina` if present in PATH.
- This is computational-only (no wet-lab instructions).
"""

import io, os, math, sys, base64, subprocess, shutil
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, html, dcc, Input, Output, State
import dash_bootstrap_components as dbc

# Optional imports
try:
    from rdkit import Chem
    from rdkit.Chem import Draw
    RDKIT = True
except Exception:
    RDKIT = False

try:
    from Bio import SeqIO
    BIOPY = True
except Exception:
    BIOPY = False

# -------------------------
# Utilities: molecule + protein parsing & preview
# -------------------------
def mol_image_base64(smiles, size=(300,300)):
    if not RDKIT: 
        return None
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        img = Draw.MolToImage(mol, size=size)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None

def parse_fasta_text(text):
    """Simple FASTA parser fallback if Biopython not available."""
    seqs = {}
    if BIOPY:
        for rec in SeqIO.parse(io.StringIO(text), "fasta"):
            seqs[rec.id] = str(rec.seq).upper()
        return seqs
    # fallback: split by '>' blocks
    blocks = [b for b in text.split('>') if b.strip()]
    for b in blocks:
        lines = b.strip().splitlines()
        header = lines[0].strip().split()[0]
        seq = ''.join(lines[1:]).strip().upper()
        seqs[header] = seq
    return seqs

# -------------------------
# Protein features (simple, fast)
# -------------------------
AMINO_ACIDS = list("ACDEFGHIKLMNPQRSTVWY")
def aa_composition(seq):
    seq = seq.upper()
    n = len(seq) if len(seq) > 0 else 1
    counts = {aa: seq.count(aa)/n for aa in AMINO_ACIDS}
    # additional simple features
    mw = sum([110.0 for _ in seq])  # rough placeholder; avoid heavy dependencies
    return {**counts, "length": n, "rough_MW": mw}

# -------------------------
# Quantum engine core (simulate Bloch vectors)
# - full Bloch vectors (X,Y,Z) generated deterministically from input string hash
# - noise parameter to simulate decoherence
# -------------------------
def quantum_engine_full(identifier, n_qubits=128, noise=0.0, seed=42):
    """
    identifier: string (SMILES, sequence, or label) -> deterministic pseudorandom pattern
    returns: DataFrame with columns [qubit, X, Y, Z]
    """
    # base rng seeded by input hash for reproducibility per-input
    h = abs(hash(str(identifier))) % (2**32)
    rng = np.random.RandomState(int(h ^ seed))
    t = np.linspace(0, 2*math.pi, n_qubits, endpoint=False)

    # create Z pattern influenced by string length / char codes
    s_val = (sum(bytearray(str(identifier), 'utf8')) % 1000) / 1000.0
    Z_base = np.sin(t * (1 + s_val*3)) * (0.6 + 0.4 * s_val)
    # add small deterministic variations
    Z = Z_base + 0.05 * rng.randn(n_qubits)
    Z = np.clip(Z, -0.9999, 0.9999)

    # X/Y from phase and small noise (allow full sphere)
    phase = np.cos(t* (1+ s_val*2)) 
    X = np.sign(phase) * np.sqrt(np.clip(1.0 - Z**2, 0.0, 1.0)) * (0.9 + 0.1*rng.randn(n_qubits))
    Y = 0.1 * rng.randn(n_qubits)  # small Y component by default
    # apply noise as decoherence: shrink vector length randomly
    if noise and noise > 0.0:
        shrink = 1.0 - np.abs(rng.normal(loc=0.0, scale=noise, size=(n_qubits,)))
        shrink = np.clip(shrink, 0.0, 1.0)
        X = X * shrink
        Y = Y * shrink
        Z = Z * shrink

    vecs = np.vstack([X, Y, Z]).T
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    df = pd.DataFrame({
        "qubit": np.arange(n_qubits),
        "X": vecs[:,0],
        "Y": vecs[:,1],
        "Z": vecs[:,2]
    })
    # fidelity heuristic
    fidelity = float(max(0.0, 1.0 - noise*0.2))
    return df, fidelity

# -------------------------
# Entanglement / similarity measures
# -------------------------
def z_product_matrix(df):
    z = df["Z"].to_numpy()
    return np.abs(np.outer(z, z))

def bloch_dot_matrix(df):
    v = df[["X","Y","Z"]].to_numpy()
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms==0] = 1.0
    u = v / norms
    M = np.abs(u @ u.T)
    return M

def feature_vector_from_molecule(smiles, n_qubits=128, noise=0.0):
    df, fidelity = quantum_engine_full(smiles, n_qubits=n_qubits, noise=noise)
    # aggregate: mean of Bloch vectors + flattened small summary
    mean_vec = df[["X","Y","Z"]].mean().to_numpy()
    stats = np.concatenate([df[["X","Y","Z"]].mean().to_numpy(),
                            df[["X","Y","Z"]].std().to_numpy(),
                            [fidelity]])
    return stats  # length 7

def feature_vector_from_protein(seq, n_qubits=128, noise=0.0):
    df, fidelity = quantum_engine_full(seq, n_qubits=n_qubits, noise=noise)
    aa_comp = aa_composition(seq)
    aa_vals = np.array([aa_comp[aa] for aa in AMINO_ACIDS])
    stats = np.concatenate([df[["X","Y","Z"]].mean().to_numpy(),
                            df[["X","Y","Z"]].std().to_numpy(),
                            [fidelity],
                            aa_vals])
    return stats  # length 7 + 20

def similarity_score(vec1, vec2):
    # cosine similarity robust
    v1 = np.array(vec1, dtype=float)
    v2 = np.array(vec2, dtype=float)
    if v1.size == 0 or v2.size == 0:
        return 0.0
    # pad or truncate to equal length
    L = min(len(v1), len(v2))
    v1 = v1[:L]
    v2 = v2[:L]
    denom = (np.linalg.norm(v1) * np.linalg.norm(v2))
    if denom == 0:
        return 0.0
    return float(np.dot(v1, v2) / denom)

# -------------------------
# Optional: AutoDock Vina wrapper (simple)
# Requires `vina` executable in PATH or conda package installed
# This wrapper just checks availability and runs with minimum args
# -------------------------
def is_vina_available():
    return shutil.which("vina") is not None

def run_vina_docking(receptor_pdbqt, ligand_pdbqt, center_x=0, center_y=0, center_z=0, size_x=20, size_y=20, size_z=20, out_pdbqt="vina_out.pdbqt"):
    """
    Minimal wrapper. Assumes receptor_pdbqt and ligand_pdbqt are prepared.
    Returns (success, stdout or error)
    """
    if not is_vina_available():
        return False, "vina not found in PATH"
    cmd = [
        "vina",
        "--receptor", receptor_pdbqt,
        "--ligand", ligand_pdbqt,
        "--center_x", str(center_x), "--center_y", str(center_y), "--center_z", str(center_z),
        "--size_x", str(size_x), "--size_y", str(size_y), "--size_z", str(size_z),
        "--out", out_pdbqt
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
        if proc.returncode != 0:
            return False, proc.stderr
        return True, proc.stdout
    except Exception as e:
        return False, str(e)

# -------------------------
# Download helpers
# -------------------------
def prepare_vectors_csv(df, mat, measure_name="bloch_dot"):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.write(f"\n#pairwise_matrix,measure={measure_name}\n")
    n = mat.shape[0]
    rows = []
    for i in range(n):
        for j in range(n):
            rows.append((i, j, float(mat[i,j])))
    pd.DataFrame(rows, columns=["i","j","value"]).to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")

# -------------------------
# Dash App UI
# -------------------------
app = Dash(__name__, external_stylesheets=[dbc.themes.FLATLY])
app.title = "Chipless Quantum Engine v5 — Full"

app.layout = dbc.Container([
    html.H2("⚛️ Chipless Quantum Engine v5 — Full (Molecule + Protein)"),
    dbc.Row([
        dbc.Col([
            html.H5("Input"),
            dcc.Dropdown(id="input_type", options=[
                {"label":"SMILES (molecule)", "value":"smiles"},
                {"label":"FASTA (protein/DNA)", "value":"fasta"},
                {"label":"Free text / label", "value":"label"}
            ], value="smiles"),
            dcc.Textarea(id="input_text", value="CuSO4", style={"width":"100%", "height":"120px"}),
            html.Br(),
            html.Label("n_qubits"),
            dcc.Slider(id="nq", min=8, max=1024, step=8, value=128),
            html.Label("noise"),
            dcc.Slider(id="noise", min=0.0, max=0.3, step=0.01, value=0.0),
            html.Label("Entanglement proxy"),
            dcc.Dropdown(id="measure", options=[
                {"label":"Bloch dot (geometry)", "value":"blochdot"},
                {"label":"Z-product (fast)", "value":"zprod"}
            ], value="blochdot"),
            html.Br(),
            dbc.Button("Run", id="run", color="primary"),
            html.Br(), html.Br(),
            html.Div(id="status_text"),
            html.Br(),
            dbc.Button("Download results (CSV)", id="dlbtn", color="secondary"),
            dcc.Download(id="download_datafile"),
            html.Br(), html.Br(),
            html.Div(id="vina_info", style={"fontSize":"0.9em", "color":"#444"})
        ], width=4),

        dbc.Col([
            dbc.Row([
                dbc.Col(html.H5("Molecule / Sequence preview"), width=6),
                dbc.Col(html.H5("Entanglement heatmap"), width=6)
            ]),
            dbc.Row([
                dbc.Col(html.Img(id="mol_img", style={"maxWidth":"320px","border":"1px solid #ccc","borderRadius":"6px"}), width=4),
                dbc.Col(dcc.Graph(id="heatmap", style={"height":"420px"}), width=8)
            ]),
            html.Hr(),
            html.H5("Bloch Vectors (3D scatter)"),
            dcc.Graph(id="bloch3d", style={"height":"520px"}),
            html.Hr(),
            html.H5("Similarity / Feature summary"),
            html.Pre(id="feat_summary", style={"whiteSpace":"pre-wrap"})
        ], width=8)
    ])
], fluid=True)

# -------------------------
# Callbacks
# -------------------------
@app.callback(
    [Output("status_text","children"),
     Output("mol_img","src"),
     Output("heatmap","figure"),
     Output("bloch3d","figure"),
     Output("feat_summary","children"),
     Output("vina_info","children")],
    Input("run","n_clicks"),
    [State("input_type","value"), State("input_text","value"), State("nq","value"), State("noise","value"), State("measure","value")]
)
def run_pipeline(nc, input_type, input_text, nq, noise, measure):
    if not nc:
        vina_msg = "Vina available: " + ("Yes" if is_vina_available() else "No")
        return "", None, go.Figure(), go.Figure(), "", vina_msg
    if not input_text or input_text.strip() == "":
        return "⚠️ Provide input text (SMILES or FASTA).", None, go.Figure(), go.Figure(), "", ""
    try:
        if input_type == "smiles":
            smiles = input_text.strip().splitlines()[0].strip()
            mol_img = mol_image_base64(smiles)
            df, fidelity = quantum_engine_full(smiles, n_qubits=int(nq), noise=float(noise))
        elif input_type == "fasta":
            seqs = parse_fasta_text(input_text)
            # take first sequence for demo
            key = list(seqs.keys())[0]
            seq = seqs[key]
            mol_img = None
            df, fidelity = quantum_engine_full(seq, n_qubits=int(nq), noise=float(noise))
        else:
            label = input_text.strip().splitlines()[0].strip()
            mol_img = None
            df, fidelity = quantum_engine_full(label, n_qubits=int(nq), noise=float(noise))

        # choose matrix
        if measure == "zprod":
            mat = z_product_matrix(df)
            measure_name = "Z-product"
        else:
            mat = bloch_dot_matrix(df)
            measure_name = "Bloch-dot"

        # heatmap
        fig_heat = go.Figure(data=go.Heatmap(z=mat, colorscale="Viridis"))
        fig_heat.update_layout(title=f"Entanglement proxy: {measure_name}", height=420)

        fig3 = go.Figure(data=[go.Scatter3d(x=df["X"], y=df["Y"], z=df["Z"], mode='markers',
                                           marker=dict(size=3, color=df["Z"], colorscale='Plasma', colorbar=dict(title="Z")) )])
        fig3.update_layout(title="Bloch vectors (3D)", scene=dict(xaxis_title="X", yaxis_title="Y", zaxis_title="Z"))

        # feature summary (aggregate)
        mean_xyz = df[["X","Y","Z"]].mean().to_dict()
        std_xyz = df[["X","Y","Z"]].std().to_dict()
        feat_text = f"Estimated fidelity: {fidelity:.4f}\nMean XYZ: {mean_xyz}\nStd XYZ: {std_xyz}\nQuBits: {len(df)}\nMeasure: {measure_name}"
        vina_msg = "Vina available: " + ("Yes" if is_vina_available() else "No")
        return f"✅ Done — fidelity {fidelity:.4f}", mol_img, fig_heat, fig3, feat_text, vina_msg
    except Exception as e:
        return f"❌ Error: {e}", None, go.Figure(), go.Figure(), "", ""

# download callback
@app.callback(
    Output("download_datafile","data"),
    Input("dlbtn","n_clicks"),
    [State("input_type","value"), State("input_text","value"), State("nq","value"), State("noise","value"), State("measure","value")],
    prevent_initial_call=True
)
def download_cb(nc, input_type, input_text, nq, noise, measure):
    if not input_text:
        return None
    # recompute
    if input_type == "smiles":
        identifier = input_text.strip().splitlines()[0].strip()
    elif input_type == "fasta":
        seqs = parse_fasta_text(input_text)
        identifier = list(seqs.keys())[0]
    else:
        identifier = input_text.strip().splitlines()[0].strip()
    df, fidelity = quantum_engine_full(identifier, n_qubits=int(nq), noise=float(noise))
    mat = bloch_dot_matrix(df) if measure=="blochdot" else z_product_matrix(df)
    csv_bytes = prepare_vectors_csv(df, mat, measure_name=("bloch_dot" if measure=="blochdot" else "z_product"))
    filename = f"chipless_v5full_{identifier[:20].replace('/','_')}.csv"
    return dcc.send_bytes(csv_bytes, filename)

# -------------------------
# Run server
# -------------------------
if __name__ == "__main__":
    print("Starting Chipless Quantum Engine v5 Full at http://127.0.0.1:8050")
    app.run(debug=True, port=8050)
    

import os
import random
from typing import List, Tuple, Dict, Any
from tqdm import tqdm
import xml.etree.ElementTree as ET
import urllib.request
import tempfile

import torch
import pandas as pd
import numpy as np
from torch_geometric.data import Data
import networkx as nx

def load_gene_sequence(file_path: str) -> str:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Gene file not found: {file_path}")
        
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    seq = "".join([l.strip() for l in lines if not l.startswith(">")])
    return seq

def load_genes_from_dir(dir_path: str, max_length: int = 10000, augment: bool = True, num_augmentations: int = 10) -> List[Tuple[str, str]]:
    if not os.path.exists(dir_path):
        raise FileNotFoundError(f"Directory not found: {dir_path}")
    
    gene_files = [f for f in os.listdir(dir_path) if f.endswith(('.txt', '.fasta'))]
    result = []
    
    print(f"Loading {len(gene_files)} genes from {dir_path}")
    for file_name in tqdm(gene_files, desc="Loading genes"):
        file_path = os.path.join(dir_path, file_name)
        gene_name = os.path.splitext(file_name)[0].upper()
        original_seq = load_gene_sequence(file_path)
        original_seq = original_seq.upper().replace('U', 'T')
        assert ">" not in original_seq, f"Header character '>' found in sequence from {file_path}"
        if len(original_seq) > max_length:
            original_seq = original_seq[:max_length]
        else:
            original_seq += "N" * (max_length - len(original_seq))
      
        result.append((gene_name, original_seq))
        
        if augment:
            for i in range(1, num_augmentations):
                mutation_rate = 0.001 * (0.8 + 0.4 * (i / num_augmentations))
                deletion_rate = 0.0005 * (0.8 + 0.4 * (i / num_augmentations))
                augmented_seq = apply_augmentations(
                    original_seq, 
                    mutation_rate=mutation_rate,
                    deletion_rate=deletion_rate
                )
                aug_name = f"{gene_name}_aug{i}"
                result.append((aug_name, augmented_seq))
    
    print(f"Generated dataset with {len(result)} sequences ({len(gene_files)} original + {len(result) - len(gene_files)} augmented)")
    return result

def load_gene_sequences(file_path: str, max_length: int = 10000, augment: bool = True) -> str:
    seq = load_gene_sequence(file_path)
    
    seq = seq.upper().replace('U', 'T') 
    assert ">" not in seq, f"Header character '>' found in sequence from {file_path}"
    if augment:
        seq = apply_augmentations(seq, mutation_rate=0.001, deletion_rate=0.0005)
    if len(seq) > max_length:
        seq = seq[:max_length]
    else:
        seq += "N" * (max_length - len(seq))
    assert len(seq) == max_length, f"Sequence length mismatch: {len(seq)} != {max_length}"
    
    return seq

def apply_augmentations(seq: str, mutation_rate: float, deletion_rate: float) -> str:
    mutation_rate = mutation_rate * 10 
    deletion_rate = deletion_rate * 10 
    
    nucleotides = ['A', 'C', 'G', 'T']
    seq_list = list(seq)
    original_length = len(seq_list)
    new_seq = []
    deleted_count = 0
    
    for base in seq_list:
        if random.random() < deletion_rate:
            deleted_count += 1
            continue
            
        if random.random() < mutation_rate:
            base = random.choice(nucleotides)
            
        new_seq.append(base)
    while len(new_seq) < original_length:
        new_seq.append(random.choice(nucleotides))
    
    return "".join(new_seq)

def load_pathway_graph(kegg_path: str) -> Data:
    if not os.path.exists(kegg_path):
        raise FileNotFoundError(f"Pathway file not found: {kegg_path}")
        
    df = pd.read_csv(kegg_path, sep='\t')
    nodes = list(set(df['source']).union(df['target']))
    node_to_idx = {n: i for i, n in enumerate(nodes)}
    degrees = {n: 0 for n in nodes}
    edges = []
    for _, row in df.iterrows():
        src = node_to_idx[row['source']]
        tgt = node_to_idx[row['target']]
        edges.append([src, tgt])
        degrees[row['source']] += 1
        degrees[row['target']] += 1
    edge_index = torch.tensor(edges, dtype=torch.long).t().contiguous()
    x_list = []
    for node in nodes:
        deg = degrees[node]
        sign_rows = df[(df['source'] == node) | (df['target'] == node)]
        if 'interactionType' in sign_rows.columns:
            sign_values = sign_rows['interactionType'].apply(
                lambda x: 1 if x == 'activation' else (-1 if x == 'inhibition' else 0)
            ).mean()
        else:
            sign_values = 0.0
        if 'goTerms' in sign_rows.columns and not sign_rows['goTerms'].isnull().all():
            go_count = sign_rows['goTerms'].apply(
                lambda x: len(str(x).split(',')) if pd.notnull(x) else 0
            ).mean()
        else:
            go_count = 0.0
            
        x_list.append([deg, sign_values, go_count])
    x = torch.tensor(x_list, dtype=torch.float)
    data = Data(x=x, edge_index=edge_index)
    return data

def load_kegg_pathway(pathway_id: str = "hsa03440", local_file: str = None) -> Data:
    if local_file and os.path.exists(local_file):
        print(f"Loading KEGG pathway from local file: {local_file}")
        kgml_file = local_file
        temp_file = None
    else:
        print(f"Downloading KEGG pathway: {pathway_id}")
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xml")
        temp_file.close()  
        url = f"https://rest.kegg.jp/get/{pathway_id}/kgml"
        urllib.request.urlretrieve(url, temp_file.name)
        kgml_file = temp_file.name
    try:
        tree = ET.parse(kgml_file)
        root = tree.getroot()
        G = nx.DiGraph(name=pathway_id)
        entries: Dict[str, Dict[str, Any]] = {}
        for entry in root.findall("./entry"):
            entry_id = entry.get("id")
            name = entry.get("name")
            entry_type = entry.get("type", "undefined")
            gene_names = name.replace("hsa:", "").split()
            primary_gene = gene_names[0] if gene_names else f"node_{entry_id}"
            ec_number = None
            for graphics in entry.findall("./graphics"):
                if "name" in graphics.attrib:
                    name_text = graphics.get("name", "")
                    if "EC:" in name_text:
                        ec_parts = name_text.split("EC:")
                        if len(ec_parts) > 1:
                            ec_number = ec_parts[1].strip()
            entries[entry_id] = {
                "id": entry_id,
                "name": primary_gene,
                "type": entry_type,
                "all_genes": gene_names,
                "ec_number": ec_number
            }
            
            G.add_node(entry_id, name=primary_gene, type=entry_type, 
                       all_genes=gene_names, ec_number=ec_number)
        
        for relation in root.findall("./relation"):
            entry1 = relation.get("entry1")
            entry2 = relation.get("entry2")
            rel_type = relation.get("type", "undefined")
            
            if entry1 not in entries or entry2 not in entries:
                continue
            
            subtypes = []
            effect = 0 
            
            for subtype_elem in relation.findall("./subtype"):
                subtype_name = subtype_elem.get("name", "undefined")
                subtypes.append(subtype_name)
                
                activation_subtypes = ["activation", "expression", "phosphorylation", 
                                     "indirect effect", "binding/association"]
                inhibition_subtypes = ["inhibition", "repression", "dephosphorylation", 
                                     "ubiquitination", "methylation"]
                
                if subtype_name in activation_subtypes:
                    effect = 1.0  
                elif subtype_name in inhibition_subtypes:
                    effect = -1.0 
            subtype_str = "|".join(subtypes) if subtypes else "undefined"
            G.add_edge(entry1, entry2, type=rel_type, subtypes=subtypes,
                      subtype_str=subtype_str, effect=effect)
        degree_centrality = nx.degree_centrality(G)
        betweenness_centrality = nx.betweenness_centrality(G)
        subtype_to_idx = {}
        idx_counter = 0
        for _, _, edge_data in G.edges(data=True):
            for subtype in edge_data.get("subtypes", []):
                if subtype not in subtype_to_idx:
                    subtype_to_idx[subtype] = idx_counter
                    idx_counter += 1
        node_names = []
        node_features = []
        for node_id in G.nodes():
            node_data = G.nodes[node_id]
            node_names.append(node_data["name"])
            if node_data["type"] == "gene":
                type_code = 0
            elif node_data["type"] == "compound":
                type_code = 1
            else:
                type_code = 2
            outgoing_edges = list(G.out_edges(node_id, data=True))
            if outgoing_edges:
                effects = [e[2].get("effect", 0) for e in outgoing_edges]
                avg_effect = sum(effects) / len(effects)
            else:
                avg_effect = 0
            has_ec = 1.0 if node_data.get("ec_number") else 0.0
            features = [
                degree_centrality[node_id], 
                has_ec,                    
                avg_effect,                
                betweenness_centrality[node_id] 
            ]
            
            node_features.append(features)
        edge_index = []
        edge_features = []
        
        for u, v, data in G.edges(data=True):
            u_idx = list(G.nodes()).index(u)
            v_idx = list(G.nodes()).index(v)
            
            edge_index.append([u_idx, v_idx])
            rel_type = data.get("type", "undefined")
            if rel_type == "PPrel":   
                rel_type_code = 0
            elif rel_type == "GErel":  
                rel_type_code = 1
            elif rel_type == "PCrel":   
                rel_type_code = 2  
            elif rel_type == "ECrel":    
                rel_type_code = 3
            else:
                rel_type_code = 4
                
            edge_feat = [
                data.get("effect", 0), 
                rel_type_code         
            ]
            
            edge_features.append(edge_feat)

        x = torch.tensor(node_features, dtype=torch.float)
        edge_index = torch.tensor(edge_index, dtype=torch.long).t().contiguous()
        edge_attr = torch.tensor(edge_features, dtype=torch.float)
        data = Data(
            x=x, 
            edge_index=edge_index, 
            edge_attr=edge_attr,
            node_names=node_names
        )
        
        print(f"KEGG pathway graph: {len(node_names)} nodes, {len(edge_index[0])} edges")
        print(f"Node feature dimensions: {x.shape}")
        print(f"Edge feature dimensions: {edge_attr.shape}")
        
        return data
        
    except Exception as e:
        print(f"Error processing KEGG pathway: {e}")
        raise
    finally:
        if temp_file:
            try:
                os.unlink(temp_file.name)
            except (PermissionError, OSError):
                print(f"Note: Could not delete temporary file, it will be removed later")

def augment_sequence(seq: str, mutation_rate: float = 0.1) -> str:
    if not seq:
        return seq
        
    if mutation_rate <= 0:
        return seq
        
    nucleotides = ['A', 'T', 'G', 'C']
    seq_list = list(seq.upper())
    seq_length = len(seq_list)
    
    num_mutations = max(1, int(seq_length * mutation_rate))
    
    positions = random.sample(range(seq_length), num_mutations)
    
    for pos in positions:
        current = seq_list[pos]
        alternatives = [n for n in nucleotides if n != current]
        
        if current not in nucleotides:
            seq_list[pos] = random.choice(nucleotides)
        else:
            seq_list[pos] = random.choice(alternatives)
    
    mutated_seq = ''.join(seq_list)
    assert len(mutated_seq) == len(seq), "Mutated sequence length must match original"
    
    return mutated_seq

def prepare_dna_sequence(sequence_data, tokenizer, max_length=10000):
    encoding = tokenizer(
        sequence_data,
        return_tensors="pt",
        padding="max_length",
        max_length=max_length,
        truncation=True
    )
    return encoding.input_ids

def test_augmentation():
    sample_seq = "ATGCATGCATGCATGCATGC" * 5 
    augmented = augment_sequence(sample_seq, mutation_rate=0.1)
    
    print(f"Original length: {len(sample_seq)}")
    print(f"Augmented length: {len(augmented)}")
    
    diff_count = sum(1 for a, b in zip(sample_seq, augmented) if a != b)
    diff_percentage = diff_count / len(sample_seq) * 100
    
    print(f"Differences: {diff_count} ({diff_percentage:.1f}%)")
    print(f"Expected mutations: {int(len(sample_seq) * 0.1)}")
    
    brca1_path = "src/data/pos_genes/BRCA1.txt"
    if os.path.exists(brca1_path):
        with open(brca1_path, "r") as f:
            brca1_seq = f.read().strip()
        
        augmented_brca1 = augment_sequence(brca1_seq, mutation_rate=0.1)
        print(f"\nBRCA1 original length: {len(brca1_seq)}")
        print(f"BRCA1 augmented length: {len(augmented_brca1)}")
        
        assert len(brca1_seq) == len(augmented_brca1), "Lengths must match"
        print("BRCA1 augmentation validation: PASSED")


if __name__ == "__main__":
    test_augmentation()
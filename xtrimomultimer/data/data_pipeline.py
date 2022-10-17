# Copyright 2022 BioMap (Beijing) Intelligence Technology Limited
# Copyright 2021 AlQuraishi Laboratory
# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from multiprocessing import cpu_count
from typing import Dict, List, Mapping, Optional, Sequence, Any
import numpy as np

from xtrimomultimer.data import templates, parsers, mmcif_parsing, msa_identifiers
from xtrimomultimer.data.tools.base import MSARunner, TemplateSearcher
from xtrimomultimer.utils.general_utils import to_date
from xtrimomultimer.np import residue_constants, protein
from xtrimomultimer.data.utils.static import *
from xtrimomultimer.data.tools import jackhmmer, hhblits, hhsearch


from xtrimomultimer.utils.logger import Logger

logger = Logger.logger


FeatureDict = Mapping[str, np.ndarray]


def empty_template_feats(n_res: int) -> FeatureDict:
    return {
        "template_aatype": np.zeros((0, n_res)).astype(np.int64),
        "template_all_atom_positions": np.zeros((0, n_res, 37, 3)).astype(np.float32),
        "template_sum_probs": np.zeros((0, 1)).astype(np.float32),
        "template_all_atom_mask": np.zeros((0, n_res, 37)).astype(np.float32),
    }


def make_template_features(
    input_sequence: str,
    hits: Mapping[str, Any],
    template_featurizer: Optional[templates.TemplateHitFeaturizer],
    query_pdb_code: Optional[str] = None,
    query_release_date: Optional[str] = None,
) -> FeatureDict:
    hits_cat = sum(hits.values(), [])
    if len(hits_cat) == 0 or template_featurizer is None:
        template_features = empty_template_feats(len(input_sequence))
    else:
        templates_result = template_featurizer.get_templates(
            query_sequence=input_sequence,
            query_pdb_code=query_pdb_code,
            query_release_date=to_date(query_release_date)
            if query_release_date
            else None,
            hits=hits_cat,
        )
        template_features = templates_result.features

        # The template featurizer doesn't format empty template features
        # properly. This is a quick fix.
        if template_features["template_aatype"].shape[0] == 0:
            template_features = empty_template_feats(len(input_sequence))

    return template_features


def unify_template_features(
    template_feature_list: Sequence[FeatureDict],
) -> FeatureDict:
    out_dicts = []
    seq_lens = [fd["template_aatype"].shape[1] for fd in template_feature_list]
    for i, fd in enumerate(template_feature_list):
        out_dict = {}
        n_templates, n_res = fd["template_aatype"].shape[:2]
        for k, v in fd.items():
            seq_keys = [
                "template_aatype",
                "template_all_atom_positions",
                "template_all_atom_mask",
            ]
            if k in seq_keys:
                new_shape = list(v.shape)
                assert new_shape[1] == n_res
                new_shape[1] = sum(seq_lens)
                new_array = np.zeros(new_shape, dtype=v.dtype)

                if k == "template_aatype":
                    new_array[..., residue_constants.HHBLITS_AA_TO_ID["-"]] = 1

                offset = sum(seq_lens[:i])
                new_array[:, offset : offset + seq_lens[i]] = v
                out_dict[k] = new_array
            else:
                out_dict[k] = v

        chain_indices = np.array(n_templates * [i])
        out_dict["template_chain_index"] = chain_indices

        if n_templates != 0:
            out_dicts.append(out_dict)

    if len(out_dicts) > 0:
        out_dict = {
            k: np.concatenate([od[k] for od in out_dicts]) for k in out_dicts[0]
        }
    else:
        out_dict = empty_template_feats(sum(seq_lens))

    return out_dict


def make_sequence_features(
    sequence: str, description: str, num_res: int
) -> FeatureDict:
    """Construct a feature dict of sequence features."""
    features = {}
    features["aatype"] = residue_constants.sequence_to_onehot(
        sequence=sequence,
        mapping=residue_constants.restype_order_with_x,
        map_unknown_to_x=True,
    )
    features["between_segment_residues"] = np.zeros((num_res,), dtype=np.int32)
    features["domain_name"] = np.array([description.encode("utf-8")], dtype=np.object_)
    features["residue_index"] = np.array(range(num_res), dtype=np.int32)
    features["seq_length"] = np.array([num_res] * num_res, dtype=np.int32)
    features["sequence"] = np.array([sequence.encode("utf-8")], dtype=np.object_)
    return features


def make_mmcif_features(
    mmcif_object: mmcif_parsing.MmcifObject, chain_id: str
) -> FeatureDict:
    input_sequence = mmcif_object.chain_to_seqres[chain_id]
    description = "_".join([mmcif_object.file_id, chain_id])
    num_res = len(input_sequence)

    mmcif_feats = {}
    mmcif_feats.update(
        make_sequence_features(
            sequence=input_sequence, description=description, num_res=num_res
        )
    )

    all_atom_positions, all_atom_mask = mmcif_parsing.get_atom_coords(
        mmcif_object=mmcif_object, chain_id=chain_id
    )
    mmcif_feats["all_atom_positions"] = all_atom_positions
    mmcif_feats["all_atom_mask"] = all_atom_mask
    mmcif_feats["resolution"] = np.array(
        [mmcif_object.header["resolution"]], dtype=np.float32
    )
    mmcif_feats["release_date"] = np.array(
        [mmcif_object.header["release_date"].encode("utf-8")], dtype=np.object_
    )
    mmcif_feats["is_distillation"] = np.array(0.0, dtype=np.float32)

    return mmcif_feats


def _aatype_to_str_sequence(aatype):
    return "".join(
        [residue_constants.restypes_with_x[aatype[i]] for i in range(len(aatype))]
    )


def make_protein_features(
    protein_object: protein.Protein, description: str, _is_distillation: bool = False
) -> FeatureDict:
    pdb_feats = {}
    aatype = protein_object.aatype
    sequence = _aatype_to_str_sequence(aatype)
    pdb_feats.update(
        make_sequence_features(
            sequence=sequence,
            description=description,
            num_res=len(protein_object.aatype),
        )
    )

    all_atom_positions = protein_object.atom_positions
    all_atom_mask = protein_object.atom_mask

    pdb_feats["all_atom_positions"] = all_atom_positions.astype(np.float32)
    pdb_feats["all_atom_mask"] = all_atom_mask.astype(np.float32)

    pdb_feats["resolution"] = np.array([0.0]).astype(np.float32)
    pdb_feats["is_distillation"] = np.array(1.0 if _is_distillation else 0.0).astype(
        np.float32
    )

    return pdb_feats


def make_pdb_features(
    protein_object: protein.Protein,
    description: str,
    confidence_threshold: float = 50.0,
    is_distillation: bool = True,
) -> FeatureDict:
    pdb_feats = make_protein_features(
        protein_object, description, _is_distillation=True
    )

    if is_distillation:
        high_confidence = protein_object.b_factors > confidence_threshold
        high_confidence = np.any(high_confidence, axis=-1)
        pdb_feats["all_atom_mask"] *= high_confidence[..., None]

    return pdb_feats


def make_msa_features(
    msas: Sequence[parsers.Msa],
) -> FeatureDict:
    """Constructs a feature dict of MSA features."""
    if not msas:
        raise ValueError("At least one MSA must be provided.")

    int_msa = []
    deletion_matrix = []
    uniprot_accession_ids = []
    species_ids = []
    seen_sequences = set()
    for msa_index, msa in enumerate(msas):
        if not msa:
            raise ValueError(f"MSA {msa_index} must contain at least one sequence.")
        for sequence_index, sequence in enumerate(msa.sequences):
            if sequence in seen_sequences:
                continue
            seen_sequences.add(sequence)
            int_msa.append(
                [residue_constants.HHBLITS_AA_TO_ID[res] for res in sequence]
            )
            deletion_matrix.append(msa.deletion_matrix[sequence_index])
            identifiers = msa_identifiers.get_identifiers(
                msa.descriptions[sequence_index]
            )
            uniprot_accession_ids.append(
                identifiers.uniprot_accession_id.encode("utf-8")
            )
            species_ids.append(identifiers.species_id.encode("utf-8"))

    num_res = len(msas[0].sequences[0])
    num_alignments = len(int_msa)
    features = {}
    features["deletion_matrix_int"] = np.array(deletion_matrix, dtype=np.int32)
    features["msa"] = np.array(int_msa, dtype=np.int32)
    features["num_alignments"] = np.array([num_alignments] * num_res, dtype=np.int32)
    # add in tag2.1.0
    features["msa_uniprot_accession_identifiers"] = np.array(
        uniprot_accession_ids, dtype=np.object_
    )
    features["msa_species_identifiers"] = np.array(species_ids, dtype=np.object_)
    return features


def run_msa_tool(
    msa_runner: MSARunner,
    fasta_path: str,
    msa_out_path: str,
    msa_format: List[str],
    max_sequences: Optional[int] = None,
) -> Mapping[str, Any]:
    """Runs an MSA tool, checking if output already exists first."""
    if "sto" in msa_format:
        sto_result = msa_runner.query(fasta_path, max_sequences)[0]
        if "a3m" in msa_format:
            msa_as_a3m = parsers.convert_stockholm_to_a3m(
                sto_result["sto"],
                max_sequences=max_sequences,
            )
            a3m_out_path = msa_out_path + ".a3m"
            with open(a3m_out_path, "w") as f:
                f.write(msa_as_a3m)
        sto_out_path = msa_out_path + ".sto"
        with open(sto_out_path, "w") as f:
            f.write(sto_result["sto"])
        return sto_result
    elif "a3m" in msa_format:
        result = msa_runner.query(fasta_path)[0]

        a3m_out_path = msa_out_path + ".a3m"
        with open(a3m_out_path, "w") as f:
            f.write(result["a3m"])

        return result
    else:
        raise ValueError(f"Cannot find valid msa format in: {msa_format}")


class AlignmentRunner:
    """Runs alignment tools and saves the results"""

    def __init__(
        self,
        jackhmmer_binary_path: Optional[str] = None,
        hhblits_binary_path: Optional[str] = None,
        hhsearch_binary_path: Optional[str] = None,
        uniref90_database_path: Optional[str] = None,
        mgnify_database_path: Optional[str] = None,
        bfd_database_path: Optional[str] = None,
        uniclust30_database_path: Optional[str] = None,
        uniprot_database_path: Optional[str] = None,
        pdb70_database_path: Optional[str] = None,
        template_searcher: Optional[TemplateSearcher] = None,
        use_small_bfd: Optional[bool] = None,
        no_cpus: Optional[int] = None,
        uniref_max_hits: int = 10000,
        mgnify_max_hits: int = 5000,
        uniprot_max_hits: int = 50000,
    ):
        """
        Args:
            jackhmmer_binary_path:
                Path to jackhmmer binary
            hhblits_binary_path:
                Path to hhblits binary
            hhsearch_binary_path:
                Path to hhsearch binary
            uniref90_database_path:
                Path to uniref90 database. If provided, jackhmmer_binary_path
                must also be provided
            mgnify_database_path:
                Path to mgnify database. If provided, jackhmmer_binary_path
                must also be provided
            bfd_database_path:
                Path to BFD database. Depending on the value of use_small_bfd,
                one of hhblits_binary_path or jackhmmer_binary_path must be
                provided.
            uniclust30_database_path:
                Path to uniclust30. Searched alongside BFD if use_small_bfd is
                false.
            pdb70_database_path:
                Path to pdb70 database.
            use_small_bfd:
                Whether to search the BFD database alone with jackhmmer or
                in conjunction with uniclust30 with hhblits.
            no_cpus:
                The number of CPUs available for alignment. By default, all
                CPUs are used.
            uniref_max_hits:
                Max number of uniref hits
            mgnify_max_hits:
                Max number of mgnify hits
        """
        db_map = {
            "jackhmmer": {
                "binary": jackhmmer_binary_path,
                "dbs": [
                    uniref90_database_path,
                    mgnify_database_path,
                    bfd_database_path if use_small_bfd else None,
                    uniprot_database_path,
                ],
            },
            "hhblits": {
                "binary": hhblits_binary_path,
                "dbs": [
                    bfd_database_path if not use_small_bfd else None,
                ],
            },
            "hhsearch": {
                "binary": hhsearch_binary_path,
                "dbs": [
                    pdb70_database_path,
                ],
            },
        }

        for name, dic in db_map.items():
            binary, dbs = dic["binary"], dic["dbs"]
            if binary is None and not all([x is None for x in dbs]):
                raise ValueError(f"{name} DBs provided but {name} binary is None")

        if (
            not all([x is None for x in db_map["hhsearch"]["dbs"]])
            and uniref90_database_path is None
        ):
            raise ValueError(
                """uniref90_database_path must be specified in order to perform
                   template search"""
            )

        self.uniref_max_hits = uniref_max_hits
        self.mgnify_max_hits = mgnify_max_hits
        self.uniprot_max_hits = uniprot_max_hits
        self.use_small_bfd = use_small_bfd

        if no_cpus is None:
            no_cpus = cpu_count()

        self.jackhmmer_uniref90_runner = None
        if jackhmmer_binary_path is not None and uniref90_database_path is not None:
            self.jackhmmer_uniref90_runner = jackhmmer.Jackhmmer(
                binary_path=jackhmmer_binary_path,
                database_path=uniref90_database_path,
                n_cpu=no_cpus,
            )

        self.jackhmmer_small_bfd_runner = None
        self.hhblits_bfd_uniclust_runner = None
        if bfd_database_path is not None:
            if use_small_bfd:
                self.jackhmmer_small_bfd_runner = jackhmmer.Jackhmmer(
                    binary_path=jackhmmer_binary_path,
                    database_path=bfd_database_path,
                    n_cpu=no_cpus,
                )
            else:
                dbs = [bfd_database_path]
                if uniclust30_database_path is not None:
                    dbs.append(uniclust30_database_path)
                self.hhblits_bfd_uniclust_runner = hhblits.HHBlits(
                    binary_path=hhblits_binary_path,
                    databases=dbs,
                    n_cpu=no_cpus,
                )

        self.jackhmmer_mgnify_runner = None
        if mgnify_database_path is not None:
            self.jackhmmer_mgnify_runner = jackhmmer.Jackhmmer(
                binary_path=jackhmmer_binary_path,
                database_path=mgnify_database_path,
                n_cpu=no_cpus,
            )

        self.hhsearch_pdb70_runner = None
        if pdb70_database_path is not None:
            self.hhsearch_pdb70_runner = hhsearch.HHSearch(
                binary_path=hhsearch_binary_path,
                databases=[pdb70_database_path],
                n_cpu=no_cpus,
            )

        self.jackhmmer_uniprot_runner = None
        if uniprot_database_path is not None:
            self.jackhmmer_uniprot_runner = jackhmmer.Jackhmmer(
                binary_path=jackhmmer_binary_path, database_path=uniprot_database_path
            )

        if template_searcher is not None and self.jackhmmer_uniref90_runner is None:
            raise ValueError("Uniref90 runner must be specified to run template search")

        self.template_searcher = template_searcher

    def run(
        self,
        fasta_path: str,
        output_dir: str,
    ):
        """Runs alignment tools on a sequence"""
        if self.jackhmmer_uniref90_runner is not None:
            uniref90_out_base_path = os.path.join(output_dir, "uniref90_hits")

            jackhmmer_uniref90_result = run_msa_tool(
                msa_runner=self.jackhmmer_uniref90_runner,
                fasta_path=fasta_path,
                msa_out_path=uniref90_out_base_path,
                msa_format=["sto"],
                max_sequences=self.uniref_max_hits,
            )

            template_msa = jackhmmer_uniref90_result["sto"]
            template_msa = parsers.deduplicate_stockholm_msa(template_msa)
            template_msa = parsers.remove_empty_columns_from_stockholm_msa(template_msa)

            if self.template_searcher is not None:
                if self.template_searcher.input_format == "sto":
                    pdb_templates_result = self.template_searcher.query(
                        template_msa, output_dir=output_dir
                    )
                elif self.template_searcher.input_format == "a3m":
                    uniref90_msa_as_a3m = parsers.convert_stockholm_to_a3m(template_msa)
                    pdb_templates_result = self.template_searcher.query(
                        uniref90_msa_as_a3m, output_dir=output_dir
                    )
                else:
                    fmt = self.template_searcher.input_format
                    raise ValueError(f"Unrecognized template input format: {fmt}")

            if self.hhsearch_pdb70_runner is not None:
                hhsearch_result = self.hhsearch_pdb70_runner.query(uniref90_msa_as_a3m)
                pdb70_out_path = os.path.join(output_dir, "pdb70_hits.hhr")
                with open(pdb70_out_path, "w") as f:
                    f.write(hhsearch_result)

        if self.jackhmmer_mgnify_runner is not None:
            mgnify_out_path = os.path.join(output_dir, "mgnify_hits")
            jackhmmer_mgnify_result = run_msa_tool(
                msa_runner=self.jackhmmer_mgnify_runner,
                fasta_path=fasta_path,
                msa_out_path=mgnify_out_path,
                msa_format=["sto", "a3m"],
                max_sequences=self.mgnify_max_hits,
            )

        if self.use_small_bfd and self.jackhmmer_small_bfd_runner is not None:
            bfd_out_path = os.path.join(output_dir, "small_bfd_hits")
            jackhmmer_small_bfd_result = run_msa_tool(
                msa_runner=self.jackhmmer_small_bfd_runner,
                fasta_path=fasta_path,
                msa_out_path=bfd_out_path,
                msa_format=["sto"],
            )
        elif self.hhblits_bfd_uniclust_runner is not None:
            bfd_out_path = os.path.join(output_dir, "bfd_uniclust_hits")
            hhblits_bfd_uniclust_result = run_msa_tool(
                msa_runner=self.hhblits_bfd_uniclust_runner,
                fasta_path=fasta_path,
                msa_out_path=bfd_out_path,
                msa_format=["a3m"],
            )

        if self.jackhmmer_uniprot_runner is not None:
            uniprot_out_path = os.path.join(output_dir, "uniprot_hits.sto")
            jackhmmer_uniprot_result = run_msa_tool(
                self.jackhmmer_uniprot_runner,
                fasta_path=fasta_path,
                msa_out_path=uniprot_out_path,
                msa_format=["sto"],
                max_sequences=self.uniprot_max_hits,
            )


class DataPipeline:
    """Assembles input features."""

    def __init__(
        self,
        template_featurizer: Optional[templates.TemplateHitFeaturizer],
    ):
        self.template_featurizer = template_featurizer

    def _parse_msa_data(
        self,
        alignment_dir: str,
        alignment_index: Optional[Any] = None,
    ) -> Dict[str, Any]:
        msa_data = {}
        if alignment_index is not None:
            fp = open(os.path.join(alignment_dir, alignment_index["db"]), "rb")

            def read_msa(start, size):
                fp.seek(start)
                msa = fp.read(size).decode("utf-8")
                return msa

            for name, start, size in alignment_index["files"]:
                ext = os.path.splitext(name)[-1]

                if ext == ".a3m":
                    msa = parsers.parse_a3m(read_msa(start, size))
                    data = {"msa": msa}
                elif ext == ".sto":
                    msa = parsers.parse_stockholm(read_msa(start, size))
                    data = {"msa": msa}
                else:
                    continue

                msa_data[name] = data

            fp.close()
        else:
            for f in os.listdir(alignment_dir):
                path = os.path.join(alignment_dir, f)
                ext = os.path.splitext(f)[-1]

                if ext == ".a3m":
                    with open(path, "r") as fp:
                        msa = parsers.parse_a3m(fp.read())
                    data = {"msa": msa}
                elif ext == ".sto":
                    with open(path, "r") as fp:
                        msa = parsers.parse_stockholm(fp.read())
                    data = {"msa": msa}
                else:
                    continue

                msa_data[f] = data

        return msa_data

    def _parse_template_hits(
        self, alignment_dir: str, alignment_index: Optional[Any] = None
    ) -> Mapping[str, Any]:
        all_hits = {}
        if alignment_index is not None:
            fp = open(os.path.join(alignment_dir, alignment_index["db"]), "rb")

            def read_template(start, size):
                fp.seek(start)
                return fp.read(size).decode("utf-8")

            for (name, start, size) in alignment_index["files"]:
                ext = os.path.splitext(name)[-1]

                if ext == ".hhr":
                    hits = parsers.parse_hhr(read_template(start, size))
                    all_hits[name] = hits

            fp.close()
        else:
            for f in os.listdir(alignment_dir):
                path = os.path.join(alignment_dir, f)
                ext = os.path.splitext(f)[-1]

                if ext == ".hhr":
                    with open(path, "r") as fp:
                        hits = parsers.parse_hhr(fp.read())
                    all_hits[f] = hits

        return all_hits

    def _get_msas(
        self,
        alignment_dir: str,
        input_sequence: Optional[str] = None,
        alignment_index: Optional[str] = None,
    ) -> List[parsers.Msa]:
        msa_data = self._parse_msa_data(alignment_dir, alignment_index)
        if len(msa_data) == 0:
            if input_sequence is None:
                raise ValueError(
                    """
                    If the alignment dir contains no MSAs, an input sequence
                    must be provided.
                    """
                )
            msa_data["dummy"] = {
                "msa": parsers.Msa(
                    sequences=[input_sequence],
                    deletion_matrix=[[0 for _ in input_sequence]],
                    descriptions=[""],
                )
            }

        msas = [v["msa"] for v in msa_data.values()]

        return msas

    def _process_msa_feats(
        self,
        alignment_dir: str,
        input_sequence: Optional[str] = None,
        alignment_index: Optional[str] = None,
    ) -> Mapping[str, Any]:
        msas = self._get_msas(alignment_dir, input_sequence, alignment_index)
        msa_features = make_msa_features(
            msas=msas,
        )

        return msa_features

    def process_fasta(
        self,
        fasta_path: str,
        alignment_dir: str,
        alignment_index: Optional[str] = None,
    ) -> FeatureDict:
        """Assembles features for a single sequence in a FASTA file"""
        with open(fasta_path) as f:
            fasta_str = f.read()
        input_seqs, input_descs = parsers.parse_fasta(fasta_str)
        if len(input_seqs) != 1:
            raise ValueError(f"More than one input sequence found in {fasta_path}.")
        input_sequence = input_seqs[0]
        input_description = input_descs[0]
        num_res = len(input_sequence)

        hits = self._parse_template_hits(alignment_dir, alignment_index)
        template_features = make_template_features(
            input_sequence,
            hits,
            self.template_featurizer,
        )

        sequence_features = make_sequence_features(
            sequence=input_sequence,
            description=input_description,
            num_res=num_res,
        )

        msa_features = self._process_msa_feats(
            alignment_dir, input_sequence, alignment_index
        )

        return {**sequence_features, **msa_features, **template_features}

    def process_mmcif(
        self,
        mmcif: mmcif_parsing.MmcifObject,  # parsing is expensive, so no path
        alignment_dir: str,
        chain_id: Optional[str] = None,
        alignment_index: Optional[str] = None,
    ) -> FeatureDict:
        """
        Assembles features for a specific chain in an mmCIF object.

        If chain_id is None, it is assumed that there is only one chain
        in the object. Otherwise, a ValueError is thrown.
        """
        if chain_id is None:
            chains = mmcif.structure.get_chains()
            chain = next(chains, None)
            if chain is None:
                raise ValueError("No chains in mmCIF file")
            chain_id = chain.id

        mmcif_feats = make_mmcif_features(mmcif, chain_id)
        input_sequence = mmcif.chain_to_seqres[chain_id]
        hits = self._parse_template_hits(alignment_dir, alignment_index)
        template_features = make_template_features(
            input_sequence,
            hits,
            self.template_featurizer,
            query_release_date=to_date(mmcif.header["release_date"]),
        )

        msa_features = self._process_msa_feats(
            alignment_dir, input_sequence, alignment_index
        )

        return {**mmcif_feats, **template_features, **msa_features}

    def process_pdb(
        self,
        pdb_path: str,
        alignment_dir: str,
        is_distillation: bool = True,
        chain_id: Optional[str] = None,
        _structure_index: Optional[str] = None,
        alignment_index: Optional[str] = None,
    ) -> FeatureDict:
        """
        Assembles features for a protein in a PDB file.
        """
        if _structure_index is not None:
            db_dir = os.path.dirname(pdb_path)
            db = _structure_index["db"]
            db_path = os.path.join(db_dir, db)
            fp = open(db_path, "rb")
            _, offset, length = _structure_index["files"][0]
            fp.seek(offset)
            pdb_str = fp.read(length).decode("utf-8")
            fp.close()
        else:
            with open(pdb_path, "r") as f:
                pdb_str = f.read()

        protein_object = protein.from_pdb_string(pdb_str, chain_id)
        input_sequence = _aatype_to_str_sequence(protein_object.aatype)
        description = os.path.splitext(os.path.basename(pdb_path))[0].upper()
        pdb_feats = make_pdb_features(
            protein_object, description, is_distillation=is_distillation
        )

        hits = self._parse_template_hits(alignment_dir, alignment_index)
        template_features = make_template_features(
            input_sequence,
            hits,
            self.template_featurizer,
        )

        msa_features = self._process_msa_feats(
            alignment_dir, input_sequence, alignment_index
        )

        return {**pdb_feats, **template_features, **msa_features}

    def process_core(
        self,
        core_path: str,
        alignment_dir: str,
        alignment_index: Optional[str] = None,
    ) -> FeatureDict:
        """
        Assembles features for a protein in a ProteinNet .core file.
        """
        with open(core_path, "r") as f:
            core_str = f.read()

        protein_object = protein.from_proteinnet_string(core_str)
        input_sequence = _aatype_to_str_sequence(protein_object.aatype)
        description = os.path.splitext(os.path.basename(core_path))[0].upper()
        core_feats = make_protein_features(protein_object, description)

        hits = self._parse_template_hits(alignment_dir, alignment_index)
        template_features = make_template_features(
            input_sequence,
            hits,
            self.template_featurizer,
        )

        msa_features = self._process_msa_feats(alignment_dir, input_sequence)

        return {**core_feats, **template_features, **msa_features}

    def process_multiseq_fasta(
        self,
        fasta_path: str,
        super_alignment_dir: str,
        ri_gap: int = 200,
    ) -> FeatureDict:
        """
        Assembles features for a multi-sequence FASTA. Uses Minkyung Baek's
        hack from Twitter (a.k.a. AlphaFold-Gap).
        """
        with open(fasta_path, "r") as f:
            fasta_str = f.read()

        input_seqs, input_descs = parsers.parse_fasta(fasta_str)

        # No whitespace allowed
        input_descs = [i.split()[0] for i in input_descs]

        # Stitch all of the sequences together
        input_sequence = "".join(input_seqs)
        input_description = "-".join(input_descs)
        num_res = len(input_sequence)

        sequence_features = make_sequence_features(
            sequence=input_sequence,
            description=input_description,
            num_res=num_res,
        )

        seq_lens = [len(s) for s in input_seqs]
        total_offset = 0
        for sl in seq_lens:
            total_offset += sl
            sequence_features["residue_index"][total_offset:] += ri_gap

        msa_list = []
        for seq, desc in zip(input_seqs, input_descs):
            alignment_dir = os.path.join(super_alignment_dir, desc)
            msas = self._get_msas(alignment_dir, seq, None)
            msa_list.append(msas)

        final_msa = []
        final_deletion_mat = []
        msa_it = enumerate(zip(msa_list))
        for i, msas in msa_it:
            prec, post = sum(seq_lens[:i]), sum(seq_lens[i + 1 :])
            msas = [[prec * "-" + seq + post * "-" for seq in msa] for msa in msas]
            deletion_mats = [
                [prec * [0] + dml + post * [0] for dml in deletion_mat]
                for deletion_mat in deletion_mats
            ]

            assert len(msas[0][-1]) == len(input_sequence)

            final_msa.extend(msas)
            final_deletion_mat.extend(deletion_mats)

        msa_features = make_msa_features(
            msas=final_msa,
            deletion_matrices=final_deletion_mat,
        )

        template_feature_list = []
        for seq, desc in zip(input_seqs, input_descs):
            alignment_dir = os.path.join(super_alignment_dir, desc)
            hits = self._parse_template_hits(alignment_dir, alignment_index=None)
            template_features = make_template_features(
                seq,
                hits,
                self.template_featurizer,
            )
            template_feature_list.append(template_features)

        template_features = unify_template_features(template_feature_list)

        return {
            **sequence_features,
            **msa_features,
            **template_features,
        }

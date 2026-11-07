"""The cast: the usernames computer-controlled players go by (see bots.py).

Normal-looking, eclectic names: older English, a handful of odd Welsh ones, and a few others. The list is the one place to change them. Every
name must be a valid username (3 to 24 characters; letters, digits, spaces and _ - . only) and pass the blocklist: test_bots.py checks both.
Nobody can register under any of them (server.py asks visitor.is_reserved), so a real player never shares a name with one of these.
"""
from __future__ import annotations

CAST: tuple[str, ...] = (
    # older English
    "Wulfstan", "Ealdred", "Cuthbert_77", "Mildred.Fenn", "Aelfric", "OldSwithin", "Godiva_of_Coventry", "Leofric1057",
    "Osric", "Winifred_Tuck", "Bertram.Holt", "Dunstan_Pell", "Ethelburga", "Godric the Hermit", "Alditha", "Hob_Nailer",
    "Goodwife_Marsh", "Tobias_Cleave", "Wystan", "Aethelwine", "Edric_the_Wild", "Brother_Anselm", "Gytha_Ash", "Oswin_Tanner",
    "Percival_Thorne", "Sibyl", "Maud_of_Ely", "Tancred_Rook",
    # Welsh, some of them weird
    "Cadwaladr", "Bwci_Bach", "Ysbaddaden", "Gwyn_ap_Nudd", "Hiraeth_Hal", "Taliesin_Ffion", "Blodeuwedd", "Cwtch_Cadi",
    "Pwyll_Penfro", "Elffin_Gwyddno", "Mabon_ap_Modron", "Dilwyn_Fach", "Ceridwen_Cauldron", "Gruffydd_Gwyn", "Mererid_Mai",
    "Twm_o_r_Nant", "Idris_Cader", "Eirwen_Dda", "Tegwen_Tywyn", "Rhodri_Mawr_99", "Llew_Llaw_Gyffes", "Nos_Galan", "Cai_Hir",
    "Bronwen_Fair", "Dai_Cap",
    # and a few others
    "Tamsin_Rudd", "Jory_Penhale", "Orla.M", "Ignatius_Pym", "Marigold_Finch", "Barnaby_Quill", "Ottoline_Gray", "Fenwick_Dale",
)

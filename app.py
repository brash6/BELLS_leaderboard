from collections import Counter, defaultdict
import math
import random
import re
from typing import Any, Iterable, Literal
import numpy as np
import pandas as pd
import plotly
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import yaml
import metrics
from models import Trace
import utils

DIR_TO_SAVE_PLOTS = utils.ROOT.parent / "images"
if not DIR_TO_SAVE_PLOTS.exists():
    DIR_TO_SAVE_PLOTS = utils.OUTPUTS

MAIN_DATASET = utils.DATASETS / "bells.jsonl"


def data(
    dataset: Iterable[str] | str | None | Literal["sum"] = None,
    safeguard: Iterable[str] | str | None | Literal["sum"] = None,
    failure_mode: Iterable[str] | str | None | Literal["sum"] = None,
) -> dict[Any, metrics.ConfusionMatrix]:
    # When a keyword is given and a single string, remove this dimension from the dict
    leaderboard_data = metrics.load_leaderboard()

    if not leaderboard_data:
        st.write(
            "Leaderboard is empty. Run `python src/bells.py metrics update-leaderboard datasets/*.jsonl` to populate it."
        )
        st.stop()

    def process(
        leaderboard, index: int, value: Iterable[str] | str | None | Literal["sum"]
    ) -> dict:
        if value is None:
            return leaderboard
        elif value == "sum":
            board = defaultdict(metrics.ConfusionMatrix)
            for key, cm in leaderboard.items():
                board[key[:index] + key[index + 1 :]] += cm
            return dict(board)
        elif isinstance(value, str):
            return {
                key[:index] + key[index + 1 :]: val
                for key, val in leaderboard.items()
                if key[index] == value
            }
        else:
            return {key: val for key, val in leaderboard.items() if key[index] in value}

    # We start with the last dimension to keep the index meaningful
    leaderboard = leaderboard_data
    leaderboard = process(leaderboard, 2, failure_mode)
    leaderboard = process(leaderboard, 1, safeguard)
    leaderboard = process(leaderboard, 0, dataset)

    # If keys are 1-tuple, remove the tuple
    if all(len(key) == 1 for key in leaderboard):
        leaderboard = {key[0]: val for key, val in leaderboard.items()}

    return leaderboard


def nice_name(name: str) -> str:
    """Return a nice name for a safeguard/benchmark to show on plots."""

    if name == "groundedness":
        return "Azure groundedness"

    if name.startswith("hf_"):
        name = "HF " + name[3:]

    if name.startswith("dan-"):
        name = "DAN " + name[4:]

    name = re.sub(r"\bllm\b", "LLM", name)
    name = name.replace("lmsys", "LMSys")

    # Any -somthing should be " (something)"
    name = re.sub(r"-(\w+)", r" (\1)", name)

    name = name.replace("_", " ")
    name = name[0].upper() + name[1:]

    return name


@st.fragment()
def plot_distribution_of_nb_safeguard_detecting_prompts():
    if not MAIN_DATASET.exists():
        st.write(f"No dataset found at {MAIN_DATASET} to make this plot.")

    failure_mode = "jailbreak"

    safeguards = list(data(failure_mode=failure_mode, dataset="sum"))
    datasets_with_jailbreaks = sorted(
        {key[0] for key, cm in data(failure_mode=failure_mode).items() if cm.actual_positives()}
    )

    subplots = [[dataset] for dataset in datasets_with_jailbreaks] + [datasets_with_jailbreaks]
    names = [nice_name(dataset) for dataset in datasets_with_jailbreaks] + ["Combined"]

    fig_per_row = int(math.ceil(math.sqrt(len(subplots))))
    fig = make_subplots(
        fig_per_row,
        fig_per_row,
        subplot_titles=names,
    )
    for i, datasets in enumerate(subplots):
        evals = metrics.gather_predictions(
            (trace for trace in Trace.load_traces_iter(MAIN_DATASET) if trace.dataset in datasets),
            failure_modes=[failure_mode],
            safeguards=safeguards,
        )  # (trace, failure, safeguard, (prediction, true))

        assert np.all(evals[..., 1])  # All are jailbreaks

        predictions = evals[:, 0, :, 0]  # (trace, safeguard)
        # > 0.5 -> true, but keep nans as they are (i.e. all values are 1, 0 or nan)
        nan_mask = np.isnan(predictions)
        predictions = np.where(predictions > 0.5, 1.0, 0.0)
        predictions[nan_mask] = np.nan

        safeguard_triggered = np.nansum(predictions, axis=1)  # (trace,)

        # Plot histogram
        fig.add_trace(
            go.Histogram(
                x=safeguard_triggered,
                histnorm="probability",
                xbins=dict(size=1),
                # marker_color="blue",
                # name=names[i],
                showlegend=False,
            ),
            row=(i // fig_per_row) + 1,
            col=(i % fig_per_row) + 1,
        )
    fig.update_layout(
        # xaxis_title="Number of safeguards detecting a prompt",
        # yaxis_title="Percentage of prompts",
        # template="plotly_white",
        width=800,
        height=600,
        template="plotly_white",
    )
    fig.update_xaxes(
        range=[-0.5, len(safeguards) + 0.5],
    )
    fig.update_yaxes(
        tickformat=".0%",
    )
    fig.update_xaxes(
        col=fig_per_row // 2 + 1,
        row=fig_per_row,
        title_text="Number of safeguards detecting a prompt",
    )
    fig.update_yaxes(
        row=fig_per_row // 2 + 1,
        col=1,
        title_text="Percentage of prompts",
    )
    st.plotly_chart(fig)

    return {"distribution_of_nb_safeguard_detecting_prompts_results": fig}


@st.fragment()
def plot_each_safeguard_weak_on_one_dataset():
    failure_mode = "jailbreak"

    datasets = {
        key[0] for key, cm in data(failure_mode=failure_mode).items() if cm.actual_positives()
    }

    # X-axis: safeguard, Y-axis: performance on dataset, color: dataset

    to_show = data(failure_mode=failure_mode, dataset=datasets)
    xs = sorted({key[1] for key, cm in to_show.items() if cm.actual_positives()})
    datasets = sorted({key[0] for key in to_show})

    ys_per_dataset = {
        dataset: [to_show[(dataset, safeguard)].tpr() for safeguard in xs] for dataset in datasets
    }

    fig = go.Figure()
    for dataset in datasets:
        fig.add_trace(
            go.Scatter(
                x=[nice_name(safeguard) for safeguard in xs],
                y=ys_per_dataset[dataset],
                name=nice_name(dataset),
                mode="markers",
                marker=dict(
                    size=10,
                    # Shape: +
                    symbol="cross",
                ),
            )
        )

    # Show an horizontal line so that all safeguards have a dataset on which they perfom less than
    # i.e. max(min(ys) for ys in safeguard)
    ys_by_safeguard = {
        safeguard: [to_show[(dataset, safeguard)].accuracy() for dataset in datasets]
        for safeguard in xs
    }
    bar_y = max(min(ys) for ys in ys_by_safeguard.values())
    fig.add_shape(
        type="line",
        x0=-0.3,
        x1=len(xs) - 0.7,
        y0=bar_y,
        y1=bar_y,
        line=dict(color="black", width=2, dash="dash"),
    )

    # On the left, above the bar, add the %
    fig.add_annotation(
        x=-0.3,
        y=bar_y,
        text=f"{bar_y:.1%}",
        showarrow=False,
        xanchor="left",
        yanchor="bottom",
        # bigger
        font=dict(size=20),
    )

    # On the right, add "Every safeguard performs less than % on at least one dataset"
    fig.add_annotation(
        x=len(xs) - 0.7,
        y=bar_y,
        text=f"Every safeguard performs less than {bar_y:.1%} on at least one dataset",
        showarrow=False,
        xanchor="right",
        yanchor="bottom",
        # font=dict(size=20),
    )

    fig.update_layout(
        yaxis_title="Detection Rate",
        xaxis_title="Safeguard",
        legend_title="Dataset",
        height=600,
        width=1000,
        template="plotly_white",
    )
    st.plotly_chart(fig)

    with st.expander("Bonus") as bonus:
        st.write("""The plot shows that every safeguard fails to detect jailbreaks effectively on at least one dataset, with detection rates dropping 
                 below 34.2%. This highlights the need for more robust or specialized safeguards to address diverse vulnerabilities.""")

    return {"each_safeguard_weak_on_one_dataset_results": fig, "bonus" : bonus}


@st.fragment()
def plot_fp_fn_jailbreak():
    failure_mode = "jailbreak"
    datasets_with_positives = {
        key[0] for key, cm in data().items() if cm.tp + cm.fn > 0 and key[2] == failure_mode
    }
    datasets_with_negatives = {
        key[0] for key, cm in data().items() if cm.tn + cm.fp > 0 and key[2] == failure_mode
    }

    false_alarm_dataset = st.radio(
        "False alarm rate on",
        ["Combined", *datasets_with_negatives],
        index=0,
        horizontal=True,
        format_func=nice_name,
    )

    if false_alarm_dataset == "Combined":
        info_string = f"Combined: average accuracy over {len(datasets_with_negatives)} datasets (not weighted by dataset size)"
    else:
        info_string = get_description_for_dataset(false_alarm_dataset)

    st.info(info_string, icon="ℹ️")

    missed_detection_dataset = st.radio(
        "Missed detection rate on",
        ["Combined", *datasets_with_positives],
        index=0,
        horizontal=True,
        format_func=nice_name,
    )

    if missed_detection_dataset == "Combined":
        info_string = f"Combined: average accuracy over {len(datasets_with_positives)} datasets (not weighted by dataset size)"
    else:
        info_string = get_description_for_dataset(missed_detection_dataset)

    st.info(info_string, icon="ℹ️")

    if false_alarm_dataset == "Combined":
        false_alarm = data(failure_mode=failure_mode, dataset="sum")
    else:
        false_alarm = data(failure_mode=failure_mode, dataset=false_alarm_dataset)

    if missed_detection_dataset == "Combined":
        missed_detection = data(failure_mode=failure_mode, dataset="sum")
    else:
        missed_detection = data(failure_mode=failure_mode, dataset=missed_detection_dataset)

    safeguards = sorted(set(false_alarm) & set(missed_detection))
    xs = [false_alarm[safeguard].fpr() for safeguard in safeguards]
    ys = [missed_detection[safeguard].fnr() for safeguard in safeguards]

    openness = [
        openness
        for safeguard in safeguards
        for openness in [get_property_from_safeguard_metadata(safeguard, "openness")]
    ]
    technique = [
        technique
        for safeguard in safeguards
        for technique in [get_property_from_safeguard_metadata(safeguard, "technique")]
    ]
    color_mapping = {
        "open-source": "green",
        "open-weight": "orange",
        "closed-source": "purple",
    }
    symbol_mapping = {
        "LLM": "circle",
        "classification-model": "diamond",
        "embedding-distance": "cross",
    }
    openness_colors = [color_mapping.get(item, "black") for item in openness]
    technique_symbols = [symbol_mapping.get(item, "square") for item in technique]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="markers+text",
            text=[nice_name(s) for s in safeguards],
            textposition="top center",
            marker=dict(color=openness_colors, symbol=technique_symbols, size=10),
            showlegend=False,
        )
    )

    fig.update_layout(
        xaxis_title="False Alarm Rate",
        yaxis_title="Missed Detection Rate",
        width=800,
        height=600,
    )
    fig.update_xaxes(range=[-0.1, 1.1])
    fig.update_yaxes(range=[-0.1, 1.1])

    for technique, symbol in symbol_mapping.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(color="black", symbol=symbol),
                name=technique.replace("-", " ").capitalize(),
                legendgroup="shape",
                legendgrouptitle=dict(text="Technique"),
                showlegend=True,
            )
        )

    for openness, color in color_mapping.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(color=color, symbol="square"),
                name=openness.replace("-", " ").capitalize(),
                legendgroup="color",
                legendgrouptitle=dict(
                    text="Openness",
                ),
                showlegend=True,
            )
        )

    st.subheader("Safeguard performance (Bottom left is better)")
    st.plotly_chart(fig)

    with st.expander("Bonus") as bonus:
        st.write("""Safeguards mostly fire on different prompts, with
                    overall only 7% of the prompts that are detected by no safeguard. This indicates that
                    safeguards are complementary, and a combination of them could be a much better defense
                    if their false positive rates were low enough.""")

    return {f"jailbreak_results_{false_alarm_dataset}_{missed_detection_dataset}": fig, "bonus": bonus}


def get_property_from_safeguard_metadata(safeguard_name, property_name):
    return get_property_from_metadata(safeguard_name, property_name, utils.SAFEGUARDS)


def get_parent_name(name):
    return name.split("-")[0]


def get_description_for_dataset(dataset_name):
    description = get_property_from_metadata(dataset_name, "description", utils.BENCHMARKS)
    return f"{get_parent_name(dataset_name)}: {description}"


def get_property_from_metadata(item_name, property_name, path):
    parent_name = get_parent_name(item_name)
    if (path / parent_name / "metadata.yaml").exists():
        metadata = list(yaml.safe_load_all((path / parent_name / "metadata.yaml").read_text()))
        if len(metadata) == 1:
            return metadata[0].get(property_name, "No {property_name} found")
        else:
            for doc in metadata:
                if doc.get("name") == item_name:
                    return doc.get(property_name, "No {property_name} found")
    return "No metadata found"


@st.fragment()
def bar_plot_perf_per_jailbreak_dataset():
    failure_mode = "jailbreak"
    # failure_modes = sorted({key[-1] for key in data()})
    # if len(failure_modes) == 1:
    #     failure_mode = failure_modes[0]
    # else:
    #     failure_mode = st.radio("Failure mode", failure_modes, index=0, horizontal=True)

    safeguards = sorted({key[1] for key in data(failure_mode=failure_mode)})
    xs = [dataset for dataset in data(failure_mode=failure_mode, safeguard="sum")]
    # Put datasets ending in "normal" at the end
    xs.sort(key=lambda x: x.endswith("normal"))

    fig = go.Figure()
    for safeguard in safeguards:
        data_ = data(safeguard=safeguard, failure_mode=failure_mode)
        ys = [data_[dataset].accuracy() for dataset in xs]
        textpositions = ["inside" if y > 0.08 else "outside" for y in ys]

        fig.add_trace(
            go.Bar(
                x=[nice_name(dataset) for dataset in xs],
                y=ys,
                text=[f"{y:.1%}" for y in ys],
                textposition=textpositions,
                name=nice_name(safeguard),
            )
        )
    # Add a vertical line before the "normal" datasets
    n_harmful = sum(not x.endswith("normal") for x in xs)
    fig.add_shape(
        type="line",
        x0=n_harmful - 0.5,
        x1=n_harmful - 0.5,
        y0=-0.1,
        y1=1.2,
        line=dict(color="black", width=2, dash="dash"),
    )
    # Add text on each side of the line "{failure_mode} traces" and "Normal traces"
    fig.add_annotation(
        x=n_harmful - 0.55,
        y=1.15,
        text=f"{failure_mode} Traces".title(),
        showarrow=False,
        # Anchor right
        xanchor="right",
        yanchor="middle",
    )
    fig.add_annotation(
        x=n_harmful - 0.45,
        y=1.15,
        text="Normal Traces",
        showarrow=False,
        xanchor="left",
        yanchor="middle",
    )
    fig.update_layout(
        yaxis_title="Accuracy",
        height=600,
        width=1000,
        template="plotly_white",
    )
    st.plotly_chart(fig)

    with st.expander("Bonus") as bonus:
        st.write("""The chart demonstrates the effectiveness of various guard methods in handling both adversarial and normal inputs. 
                 While most methods handle Normal Traces well, there is a clear variability in performance when it comes to Jailbreak Traces, 
                 emphasizing the need for stronger or more targeted protection methods.
                """)

    return {"jailbreak_bars_results": fig, "bonus": bonus}


@st.fragment()
def plot_hallucinations():
    failure_mode = "hallucination"
    datasets = {key[0] for key in data(failure_mode=failure_mode)}

    dataset_for_evaluation = st.radio(
        "Dataset for evaluation",
        ["Combined", *datasets],
        index=0,
        horizontal=True,
        format_func=nice_name,
    )

    if dataset_for_evaluation == "Combined":
        info_string = f"Combined: average accuracy over {len(datasets)} datasets (not weighted by dataset size)"
    else:
        info_string = get_description_for_dataset(dataset_for_evaluation)

    st.info(info_string, icon="ℹ️")

    if dataset_for_evaluation == "Combined":
        by_safeguard = data(failure_mode=failure_mode, dataset="sum")
    else:
        by_safeguard = data(failure_mode=failure_mode, dataset=dataset_for_evaluation)

    safeguards = sorted(by_safeguard)
    xs = [by_safeguard[safeguard].fpr() for safeguard in safeguards]
    ys = [by_safeguard[safeguard].fnr() for safeguard in safeguards]

    openness = [
        openness
        for safeguard in safeguards
        for openness in [get_property_from_safeguard_metadata(safeguard, "openness")]
    ]
    technique = [
        technique
        for safeguard in safeguards
        for technique in [get_property_from_safeguard_metadata(safeguard, "technique")]
    ]
    color_mapping = {
        "open-source": "green",
        "open-weight": "orange",
        "closed-source": "purple",
    }
    symbol_mapping = {
        "LLM": "circle",
        "NLI": "diamond",
    }
    openness_colors = [color_mapping.get(item, "black") for item in openness]
    technique_symbols = [symbol_mapping.get(item, "square") for item in technique]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="markers+text",
            text=[nice_name(s) for s in safeguards],
            textposition="top center",
            marker=dict(color=openness_colors, symbol=technique_symbols, size=10),
            showlegend=False,
        )
    )

    fig.update_layout(
        xaxis_title="False Alarm Rate",
        yaxis_title="Missed Detection Rate",
        width=800,
        height=600,
    )
    fig.update_xaxes(range=[-0.1, 1.1])
    fig.update_yaxes(range=[-0.1, 1.1])

    for technique, symbol in symbol_mapping.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(color="black", symbol=symbol),
                name=technique,
                legendgroup="shape",
                legendgrouptitle=dict(text="Technique"),
                showlegend=True,
            )
        )

    for openness, color in color_mapping.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(color=color, symbol="square"),
                name=openness.replace("-", " ").capitalize(),
                legendgroup="color",
                legendgrouptitle=dict(
                    text="Openness",
                ),
                showlegend=True,
            )
        )

    st.subheader("Safeguard performance (Bottom left is better)")
    st.plotly_chart(fig)

    with st.expander("Bonus") as bonus:
        st.write("""The most effective safeguards are those located near the bottom-left, such as "Azure groundedness" and "RAGAS" 
                 which balance low missed detections and false alarms. Safeguards with high missed detection rates (like "Trulens") may need 
                 improvement, especially for critical use cases where detecting hallucinations is essential. 
                """)

    return {f"hallucination_results_{dataset_for_evaluation}": fig, "bonus": bonus}


@st.fragment()
def plot_result_tables():
    leaderboard_data = data()
    datasets = sorted({key[0] for key in leaderboard_data})
    safeguards = sorted({key[1] for key in leaderboard_data})
    failure_modes = sorted({key[2] for key in leaderboard_data})

    METRICS = {
        "Detection Rate": (lambda cm: cm.tpr(), "True Positive Rate"),
        "False Alarm Rate": (lambda cm: cm.fpr(), "False Positive Rate"),
        "Total": (lambda cm: cm.total(), "Total"),
    }

    metric: str = st.radio("Metric", list(METRICS), horizontal=True)

    def get_leaderboard_data(dataset, safeguard, failure_mode):
        try:
            cm = leaderboard_data[(dataset, safeguard, failure_mode)]
        except KeyError:
            return None

        return METRICS[metric][0](cm)

    for failure_mode in failure_modes:
        if "prompt" not in failure_mode:
        # Show a table
            df = pd.DataFrame(
                {
                    safeguard: [
                        get_leaderboard_data(dataset, safeguard, failure_mode) for dataset in datasets
                    ]
                    for safeguard in safeguards
                },
                index=datasets,
            )

            # Drop rows and cols which are all None
            df = df.dropna(axis=0, how="all")
            df = df.dropna(axis=1, how="all")

            st.write(f"### {failure_mode.title()}")
            st.write(df)
    
    with st.expander("Bonus") as bonus:
        st.write("""The table highlights the strengths and weaknesses of various safeguards across different failure modes and datasets. 
                 The "Total" option show the absolute detection results over the 400 prompts selected for each failure mode and dataset.
                """)
        
    return 0, 0



PLOTS = {
    "Each safeguard is weak on one dataset": plot_each_safeguard_weak_on_one_dataset,
    "Jailbreak Safeguard Performance": plot_fp_fn_jailbreak,
    "Jailbreak Safeguard Performance per dataset": bar_plot_perf_per_jailbreak_dataset,
    "Hallucination Safeguard Performance": plot_hallucinations,
    "Results table": plot_result_tables,
}


def main():

    st.set_page_config(layout="wide", initial_sidebar_state="collapsed")

    st.title("BELLS Leaderboard")
    st.columns(2)[0].write(
        """
    The rise of large language models (LLMs) has been accompanied by the emergence of vulnerabilities, such as jailbreaks and prompt injections, which exploit these systems to bypass constraints and induce harmful or unintended behaviors. In response, safeguards have been developed to monitor inputs and outputs, aiming to detect and mitigate these vulnerabilities. These safeguards, while promising, require robust evaluation frameworks to assess their effectiveness and generalizability.

    This leaderboard provides a comprehensive evaluation of various input-output safeguards against jailbreak attempts. Using a diverse set of datasets and metrics, it benchmarks their detection capabilities and false positive rates among a wide range of prompts and use cases.
    """
    )

    # for safeguard in set(key[1] for key in data()):
    #     st.write(f"## {nice_name(safeguard)}")
    #     st.write(get_property_from_metadata(safeguard, "description", utils.SAFEGUARDS))

    with st.sidebar:
        show_all_plots = st.checkbox("Show all plots", value=True)
        if not show_all_plots:
            plot_to_show = st.selectbox("Plot to show", list(PLOTS), index=0)
            to_show = {plot_to_show: PLOTS[plot_to_show]}
        else:
            to_show = PLOTS
    
    for name, plot in to_show.items():
        st.write(f"## {name}")
        with st.spinner(f"Crunching data for {name}"):
            plots, bonus = plot()

        st.divider()

    all_hearts = "❤️-🧡-💛-💚-💙-💜-🖤-🤍-🤎-💖-❤️‍🔥".split("-")
    heart = random.choice(all_hearts)
    st.write(
        f"Made with {heart} by Diego Dorn and Hadrien Mariaccia from the [CeSIA: Centre pour la Sécurité de l'IA](https://securite-ia.fr/)."
    )


if __name__ == "__main__":
    main()

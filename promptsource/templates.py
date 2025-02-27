import os
import random
import uuid
from collections import Counter, defaultdict
from shutil import rmtree
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pkg_resources
import yaml
from jinja2 import BaseLoader, Environment


# Truncation of jinja template variables
# 1710 = 300 words x 4.7 avg characters per word + 300 spaces
TEXT_VAR_LENGTH = 2048

# Local path to the folder containing the templates
TEMPLATES_FOLDER_PATH = pkg_resources.resource_filename(__name__, "templates")

env = Environment(loader=BaseLoader)

# Allow the python function zip()
env.globals.update(zip=zip)


def highlight(input):
    return "<span style='color: #F08080'>" + input + "</span>"


def choice(choices):
    return random.choice(choices)


def most_frequent(items):
    """Returns the set of items which appear most frequently in the input"""
    if not items:
        return
    item_counts = Counter(items).most_common()
    max_freq = item_counts[0][1]
    most_frequent_items = [c[0] for c in item_counts if c[1] == max_freq]
    return most_frequent_items


env.filters["highlight"] = highlight
env.filters["choice"] = choice
env.filters["most_frequent"] = most_frequent


class Template(yaml.YAMLObject):
    """
    A prompt template.
    """

    yaml_tag = "!Template"

    def __init__(self, name, jinja, reference, metadata=None, answer_choices=None, answer_choices_key=None):
        """
        Creates a prompt template.

        A prompt template is expressed in Jinja. It is rendered using an example
        from the corresponding Hugging Face datasets library (a dictionary). The
        separator ||| should appear once to divide the template into prompt and
        output. Generally, the prompt should provide information on the desired
        behavior, e.g., text passage and instructions, and the output should be
        a desired response.

        :param name: unique name (per dataset) for template
        :param jinja: template expressed in Jinja
        :param reference: string describing author or paper reference for template
        :param metadata: a Metadata object with template annotations
        :param answer_choices: list of strings that enumerates the possible completions
                               for templates that should be evaluated as ranked
                               completions. If None, then the template is open-ended.
                               This list is accessible from within Jinja as the
                               variable `answer_choices`.
                               TODO: Merge answer_choices and answer_choices_key
        :param answer_choices_key: Jinja expression for answer choices, or None if
                                   no answer choices

        """
        self.id = str(uuid.uuid4())
        self.name = name
        self.jinja = jinja
        self.reference = reference
        self.metadata = metadata if metadata is not None else Template.Metadata()
        self.answer_choices = answer_choices
        self.answer_choices_key = answer_choices_key

    def get_id(self):
        """
        Returns the id of the template

        :return: unique id for template
        """
        return self.id

    def get_name(self):
        """
        Returns the name of the template

        :return: unique (per dataset) name for template
        """
        return self.name

    def get_reference(self):
        """
        Returns the bibliographic reference (or author) for the template

        :return: reference as a string
        """
        return self.reference

    def get_answer_choices(self):
        """
        Returns a list of strings enumerating the possible completions for
        this template, or None if the template is open ended.

        :return: List[String]
        """
        # TODO: Replace answer_choices with answer_choices_key values
        return self.answer_choices

    def get_answer_choices_expr(self):
        """
        Returns a Jinja expression for computing the answer choices from an example.

        :return: String, or None if no answer choices
        """
        # TODO: Change to return answer_choices
        return self.answer_choices_key

    def get_answer_choices_list(self, example):
        """
        Returns a list of answer choices for a given example

        :return: list of strings, or None if get_answer_choices_expr is None
        """
        if self.get_answer_choices_expr() is None:
            return None

        jinja = self.answer_choices_key
        rtemplate = env.from_string(jinja)
        protected_example = self._escape_pipe(example)
        rendered_choices = rtemplate.render(**protected_example)
        return [self._unescape_pipe(answer_choice.strip()) for answer_choice in rendered_choices.split("|||")]

    def apply(self, example, truncate=True, highlight_variables=False):
        """
        Creates a prompt by applying this template to an example

        :param example: the dataset example to create a prompt for
        :param truncate: if True, example fields will be truncated to TEXT_VAR_LENGTH chars
        :param highlight_variables: highlight the added variables
        :return: tuple of 2 strings, for prompt and output
        """
        jinja = self.jinja

        # Truncates the prompt if needed
        if truncate:
            trunc_command = (
                f" | string | truncate({TEXT_VAR_LENGTH}) }}}}"  # Escaping curly braces requires doubling them
            )
            jinja = jinja.replace("}}", trunc_command)

        # Highlights text that was substituted for variables, if requested
        if highlight_variables:
            jinja = jinja.replace("}}", " | highlight }}")
        rtemplate = env.from_string(jinja)

        protected_example = self._escape_pipe(example)

        # Adds in answer_choices variable
        if "answer_choices" in protected_example:
            raise ValueError("Example contains the restricted key 'answer_choices'.")

        if self.answer_choices is not None:
            protected_example["answer_choices"] = self.answer_choices
        elif self.get_answer_choices_list(example) is not None:
            # TODO: Combine this all into a call to get_answer_choices_list
            protected_example["answer_choices"] = self.get_answer_choices_list(example)

        # Renders the Jinja template
        rendered_example = rtemplate.render(**protected_example)

        # Splits on the separator, and then replaces back any occurrences of the
        # separator in the original example
        return [self._unescape_pipe(part) for part in rendered_example.split("|||")]

    pipe_protector = "3ed2dface8203c4c9dfb1a5dc58e41e0"

    @classmethod
    def _escape_pipe(cls, example):
        # Replaces any occurrences of the "|||" separator in the example, which
        # which will be replaced back after splitting
        protected_example = {
            key: value.replace("|||", cls.pipe_protector) if isinstance(value, str) else value
            for key, value in example.items()
        }
        return protected_example

    @classmethod
    def _unescape_pipe(cls, string):
        # replaces back any occurrences of the separator in a string
        return string.replace(cls.pipe_protector, "|||")

    class Metadata(yaml.YAMLObject):
        """
        Metadata for a prompt template.
        """

        yaml_tag = "!TemplateMetadata"

        def __init__(
            self,
            original_task: Optional[bool] = None,
            choices_in_prompt: Optional[bool] = None,
            metrics: Optional[List[str]] = None,
        ):
            """
            Initializes template metadata.

            In the following, trivial choices are defined as Yes/No, True/False,
            etc. and nontrivial choices are other types of choices denoted in
            the answer_choices field.

            :param original_task: If True, this prompt asks a model to perform the original task designed for
                this dataset.
            :param choices_in_prompt: If True, the answer choices are included in the templates such that models
                see those choices in the input. Only applicable to classification tasks.
            :param metrics: List of strings denoting metrics to use for evaluation
            """
            self.original_task = original_task
            self.choices_in_prompt = choices_in_prompt
            self.metrics = metrics


class TemplateCollection:
    """
    This helper class wraps the DatasetTemplates class
    - Initialized the DatasetTemplates for all existing template folder
    - Give access to each DatasetTemplates
    - Provides aggregated counts over all DatasetTemplates
    """

    def __init__(self):

        # Dict of all the DatasetTemplates, key is the tuple (dataset_name, subset_name)
        self.datasets_templates: Dict[(str, Optional[str]), DatasetTemplates] = self._collect_dataset()

    @property
    def keys(self):
        return list(self.datasets_templates.keys())

    def _collect_dataset(self) -> Dict[Tuple[str, str], "DatasetTemplates"]:
        """
        Initialize a DatasetTemplates object for each templates.yaml detected in the templates folder

        Returns: a dict with key=(dataset_name, subset_name)
        """
        dataset_folders = os.listdir(TEMPLATES_FOLDER_PATH)
        dataset_folders = [folder for folder in dataset_folders if not folder.startswith(".")]

        output = {}  # format is {(dataset_name, subset_name): DatasetsTemplates}
        for dataset in dataset_folders:
            for filename in os.listdir(os.path.join(TEMPLATES_FOLDER_PATH, dataset)):
                if filename.endswith(".yaml"):
                    # If there is no sub-folder, there is no subset for this dataset
                    output[(dataset, None)] = DatasetTemplates(dataset)
                else:
                    # This is a subfolder, and its name corresponds to the subset name
                    output[(dataset, filename)] = DatasetTemplates(dataset_name=dataset, subset_name=filename)
        return output

    def get_dataset(self, dataset_name: str, subset_name: Optional[str] = None) -> "DatasetTemplates":
        """
        Return the DatasetTemplates object corresponding to the dataset name

        :param dataset_name: name of the dataset to get
        :param subset_name: name of the subset
        """
        # if the dataset does not exist, we add it
        if dataset_name not in self.keys:
            self.datasets_templates[(dataset_name, subset_name)] = DatasetTemplates(dataset_name, subset_name)

        return self.datasets_templates[(dataset_name, subset_name)]

    def get_templates_count(self) -> Dict:
        """
        Return the overall number count over all datasets

        NB: we don't breakdown datasets into subsets for the count, i.e subsets count are included
        into the dataset count
        """

        count_dict = defaultdict(int)
        for k, v in self.datasets_templates.items():
            # Subsets count towards dataset count
            count_dict[k[0]] += len(v)
        # converting to regular dict
        return dict(count_dict)


class DatasetTemplates:
    """
    Class that wraps all templates for a specific dataset/subset and implements all the helper
    functions necessary to read/write to the yaml file
    """

    TEMPLATES_KEY = "templates"
    DATASET_KEY = "dataset"
    SUBSET_KEY = "subset"
    TEMPLATE_FILENAME = "templates.yaml"

    def __init__(self, dataset_name: str, subset_name: str = None):
        self.dataset_name: str = dataset_name
        self.subset_name: str = subset_name
        # dictionary is keyed by template name.
        self.templates: Dict = self.read_from_file()

        # Mapping from template name to template id
        self.name_to_id_mapping = {}
        self.sync_mapping()

    def sync_mapping(self) -> None:
        """
        Re-compute the name_to_id_mapping to ensure it is in sync with self.templates
        """
        self.name_to_id_mapping = {template.name: template.id for template in self.templates.values()}

    @property
    def all_template_names(self) -> List[str]:
        """
        Sorted list of all templates names for this dataset
        """
        return sorted([template.name for template in self.templates.values()])

    @property
    def folder_path(self) -> str:
        if self.subset_name:
            return os.path.join(TEMPLATES_FOLDER_PATH, self.dataset_name, self.subset_name)
        else:
            return os.path.join(TEMPLATES_FOLDER_PATH, self.dataset_name)

    @property
    def yaml_path(self) -> str:
        return os.path.join(self.folder_path, self.TEMPLATE_FILENAME)

    def format_for_dump(self) -> Dict:
        """
        Create a formatted dictionary for the class attributes
        """
        formatted_dict = {self.DATASET_KEY: self.dataset_name, self.TEMPLATES_KEY: self.templates}
        if self.subset_name:
            formatted_dict[self.SUBSET_KEY] = self.subset_name
        return formatted_dict

    def read_from_file(self) -> Dict:
        """
        Reads a file containing a prompt collection.
        """

        if not os.path.exists(self.yaml_path):
            return {}
        yaml_dict = yaml.load(open(self.yaml_path, "r"), Loader=yaml.FullLoader)
        return yaml_dict[self.TEMPLATES_KEY]

    def write_to_file(self) -> None:
        """
        Writes to a file with the current prompt collection.
        """
        # Sync the mapping
        self.sync_mapping()

        # We only create the folder if a template is written
        if not os.path.exists(self.folder_path):
            os.makedirs(self.folder_path)
        yaml.dump(self.format_for_dump(), open(self.yaml_path, "w"))

    def add_template(self, template: "Template") -> None:
        """
        Adds a new template for the dataset

        :param template: template
        """
        self.templates[template.get_id()] = template

        self.write_to_file()

    def remove_template(self, template_name: str) -> None:
        """
        Deletes a template

        :param template_name: name of template to remove
        """

        # Even if we have an ID, we want to check for duplicate names
        if template_name not in self.all_template_names:
            raise ValueError(f"No template with name {template_name} for dataset {self.dataset_name} exists.")

        del self.templates[self.name_to_id_mapping[template_name]]

        if len(self.templates) == 0:
            # There is no remaining template, we can remove the entire folder
            self.delete_folder()
        else:
            # We just update the file
            self.write_to_file()

    def update_template(
        self,
        current_template_name: str,
        new_template_name: str,
        jinja: str,
        reference: str,
        metadata: Template.Metadata,
        answer_choices: List[str],
        answer_choices_key: str,
    ) -> None:
        """
        Updates a pre-existing template and writes changes

        :param current_template_name: current name of the template stored in self.templates
        :param new_template_name: new name for the template
        :param jinja: new jinja entry
        :param reference: new reference entry
        :param metadata: a Metadata object with template annotations
        :param answer_choices: new answer_choices list
        :param answer_choices_key: new answer_choices_key string
        """
        template_id = self.name_to_id_mapping[current_template_name]
        self.templates[template_id].name = new_template_name
        self.templates[template_id].jinja = jinja
        self.templates[template_id].reference = reference
        self.templates[template_id].metadata = metadata
        self.templates[template_id].answer_choices = answer_choices
        self.templates[template_id].answer_choices_key = answer_choices_key

        self.write_to_file()

    def delete_folder(self) -> None:
        """
        Delete the folder corresponding to self.folder_path
        """
        self.sync_mapping()

        rmtree(self.folder_path)

        # If it is a subset, we have to check whether to remove the dataset folder
        if self.subset_name:
            # have to check for other folders
            base_dataset_folder = os.path.join(TEMPLATES_FOLDER_PATH, self.dataset_name)
            if len(os.listdir(base_dataset_folder)) == 0:
                rmtree(base_dataset_folder)

    def __getitem__(self, template_key: str) -> "Template":
        return self.templates[self.name_to_id_mapping[template_key]]

    def __len__(self) -> int:
        return len(self.templates)


def get_templates_data_frame():
    """
    Gathers all template information into a Pandas DataFrame.

    :return: Pandas DataFrame
    """
    data = {
        "id": [],
        "dataset": [],
        "subset": [],
        "name": [],
        "reference": [],
        "original_task": [],
        "choices_in_prompt": [],
        "metrics": [],
        "answer_choices": [],
        "answer_choices_key": [],
        "jinja": [],
    }

    template_collection = TemplateCollection()

    for key in template_collection.keys:
        templates = template_collection.get_dataset(key[0], key[1])
        for template_name in templates.all_template_names:
            template = templates[template_name]
            data["id"].append(template.get_id())
            data["dataset"].append(key[0])
            data["subset"].append(key[1])
            data["name"].append(template.get_name())
            data["reference"].append(template.get_reference())
            data["original_task"].append(template.metadata.original_task)
            data["choices_in_prompt"].append(template.metadata.choices_in_prompt)
            data["metrics"].append(template.metadata.metrics)
            data["answer_choices"].append(template.get_answer_choices())
            data["answer_choices_key"].append(template.get_answer_choices_expr())
            data["jinja"].append(template.jinja)

    return pd.DataFrame(data)

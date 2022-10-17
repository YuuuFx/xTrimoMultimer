# Copyright 2022 BioMap (Beijing) Intelligence Technology Limited
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

from abc import abstractmethod, ABC
from typing import Any, Mapping, Optional, Sequence


class MSARunner(ABC):
    @abstractmethod
    def query(
        self, fasta_path: str, max_sequences: Optional[int] = None
    ) -> Sequence[Mapping[str, Any]]:
        pass


class TemplateSearcher(ABC):
    @property
    @abstractmethod
    def input_format(self):
        pass

    @property
    @abstractmethod
    def output_format(self):
        pass

    @abstractmethod
    def query(self, source: str, output_dir: Optional[str] = None) -> str:
        pass

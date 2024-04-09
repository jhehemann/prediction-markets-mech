# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2023-2024 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module implements a research agent for extracting relevant information from URLs."""

from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import re
from bs4 import BeautifulSoup, NavigableString
import faiss
from googleapiclient.discovery import build
import json
import numpy as np
import tiktoken
from openai import OpenAI
from pydantic import BaseModel, Field
import requests
from requests import Session
from typing import Any, Dict, Generator, List, Optional, Tuple, Callable
from tiktoken import encoding_for_model
import html2text
from readability import Document

import logging

from openai import OpenAI
from tqdm import tqdm


from dateutil import parser

#logging.basicConfig(level=logging.DEBUG)

NUM_URLS_PER_QUERY = 3
TEXT_CHUNK_LENGTH = 300
TEXT_CHUNK_OVERLAP = 50
MAX_CHUNKS_TOKENS_TO_SUMMARIZE = 500
MAX_TEXT_CHUNKS_TOTAL = 50
EMBEDDING_MODEL = "text-embedding-3-small"
MAX_EMBEDDING_TOKEN_INPUT = 8192
EMBEDDING_SIZE = 1536
WEEKS_TO_SCRAPE_NEWS = 4

DEFAULT_OPENAI_SETTINGS = {
    "max_compl_tokens": 500,
    "temperature": 0,
}


SUMMARIZE_CHUNKS_BULLETPOINTS = """
You are provided with bulletpoints extracted from search outputs. These search outputs were received in response to the search \
query found below. Your task is to summarize the bulletpoints into a minimalistic paragraph containing only the most relevant information. Adhere to the following 'INSTRUCTIONS'.

INSTRUCTIONS:
* Carefully read the search query
* Select only the most relevant bulletpoints from the search outputs that are useful and relevant and could help answering the search query
* An information can be considered relevant if it might support or refute the search query
* An information must also be considered relevant if it reveals a specific date or time frame when the event is expected to occur
* Summarize the relevant bulletpoints into a very concise paragraph that may help to answer the search query and reveal if the event happens and on which date the event is expected to occur
* If there are conflicting information, you must include both sides of the argument in the selected bulletpoints
* Give your response in the format specified under "OUTPUT_FORMAT"

SEARCH_OUTPUT:
```
{bullet_points}
```

SEARCH_QUERY: {input_query}
SUB_QUERIES:
- Will the event happen?
- On what date will the event happen? (DD/MM/YYYY)
- Has the event happened already?

OUTPUT_FORMAT:
* Only output the summary containing the relevant information from 'SEARCH_OUTPUT'
* The summary must be an informative but very concise paragraph.
* Respond solely with "Error", if there is no relevant information in 'SEARCH_OUTPUT'.
* Do not include any other contents in your response!
"""


PREVIOUS_SUMMARY_PROMPT = """
You are provided with search outputs from multiple sources. These search outputs were received in response to the search \
query found below. Your task is to select a collection of unique and most relevant bulletpoints that may help to answer the search query and reveal \
if the event happens and on which date the event is expected to occur. 

INSTRUCTIONS:
* Carefully read the search query
* Select only the relevant bulletpoints from the search outputs that are useful and relevant and could help answering the search query
* An information can be considered relevant if it might support or refute the search query
* An information must also be considered relevant if it reveals a specific date or time frame when the event is expected to occur
* If there are redundant bulletpoints, you must drop all exept for one. Select the most relevant by two criteria:
    - Firstly: Select the one that mentiones specific dates over the ones that mention relative dates or week days
    - Secondly: Select the one that is listed more to the bottom of the search output
* If there are conflicting information, you must include both sides of the argument in the selected bulletpoints
* Give your response in the format specified under "OUTPUT_FORMAT"

SEARCH_OUTPUT:
```
{chunks}
```

SEARCH_QUERY: {input_query}
SUB_QUERIES:
- Will the event happen?
- On what date will the event happen? (DD/MM/YYYY)
- Has the event happened already?

OUTPUT_FORMAT:
* Only output the collection of five selected unique and relevant bulletpoints with the corresponding numbers in parentheses.
* The bulletpoints should be useful and relevant and help answering the search query and sub-queries.
"""


RESEARCH_PLAN_PROMPT_TEMPLATE = """
Your goal is to prepare a research plan for {query}.

The plan must consist of {search_limit} search engine queries separated by commas.
Return ONLY the queries, separated by commas and without quotes.
The queries must be phrased as concise, but descriptive questions that will help you find relevant information about the event and its date.
"""

QUERY_RERANKING_PROMPT_TEMPLATE = """
Evaluate the queries and decide which ones will provide the best data to answer the question. Do not modify the queries.
Select only the best seven queries, in order of relevance.

OUTPUT_FORMAT:
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()"
* The JSON must contain one field: "queries"
    - "queries": A 7 item array of the generated search engine queries
* Include only the JSON object in your output
"""

# SUMMARIZE_PROMPT = """
# Your task is to summarize relevant information in 'SEARCH_OUTPUT'. \
# The summary must only contain relevant information with respect to the SEARCH_QUERY. You must adhere to the following 'INSTRUCTIONS'. 

# INSTRUCTIONS:
# * Carefully read the 'SEARCH_QUERY'
# * Select only the relevant information from 'SEARCH_OUTPUT' that is useful and relevant and could help answering the search query
# * An information can be considered relevant if it might support or refute the search query
# * Summarize the relevant information in a way that is concise and informative
# * You must not infer or add any new information, but only summarize the existing statements in an unbiased way
# * If there is conflicting information, you must include both sides of the argument in the summary
# * You must provide your response in the format specified under "OUTPUT_FORMAT"
# * Do not include any other contents in your response.

# SEARCH_QUERY: {input_query}
# SUB_QUERIES:
# - Will the event happen?
# - On what date will the event happen?
# - Has the event happened already?

# SEARCH_OUTPUT:
# ```
# {chunks}
# ```

# OUTPUT_FORMAT:
# * Only output the summary containing the relevant information from 'SEARCH_OUTPUT' with respect to the search query.
# * The summary must be structured in bullet points
# * Respond solely with "Error", if there is no relevant information in 'SEARCH_OUTPUT'.
# * Do not include any other contents in your response!
# """


# SUMMARIZE_PROMPT_WITH_DATE_AND_PARAGRAPH = """
# You are provided with text chunks from a search output. These search output was received in response to the search \
# query found below. Your task is to summarize the chunks into a minimalistic paragraph containing only the most relevant information. Adhere to the following 'INSTRUCTIONS'.

# INSTRUCTIONS:
# * Carefully read the 'SEARCH_QUERY'
# * Select only the most relevant information from 'SEARCH_OUTPUT' that is useful and relevant and could help answering the search query
# * An information can be considered relevant if it might support or refute the search query
# * If there is no relevant information in 'SEARCH_OUTPUT', solely respond with the word "Error".
# * Summarize the relevant chunks into one very concise paragraph that may help to answer the search query and reveal if the event happens and on which date the event is expected to occur
# * You must not infer or add any new information, but only summarize the existing statements in an unbiased way
# * If there is conflicting information, you must include both sides of the argument in the summary
# * If there are dates mentioned in the relevant information, you must include them in the summary.
# * You must provide your response in the format specified under "OUTPUT_FORMAT"
# * Do not include any other contents in your response.

# SEARCH_QUERY:
# {input_query}
# SUB_QUERIES:
# - Will the event happen?
# - On what date will the event happen? (DD/MM/YYYY)
# - Has the event happened already?

# SEARCH_OUTPUT:
# ```
# {chunks}
# ```

# OUTPUT_FORMAT:
# * Only output the summary containing the most relevant information from 'SEARCH_OUTPUT' that may help to answer the search query and reveal if the event happens and on which date the event is expected to occur.
# * You must not make references to the search query and its outcome, but only summarize the relevant information from 'SEARCH_OUTPUT'.
# * The summary must be an informative but very concise paragraph.
# * Respond solely with "Error", if there is no relevant information in 'SEARCH_OUTPUT'.
# * Do not include any other contents in your response!
# """


SUMMARIZE_PROMPT_WITH_DATE = """
Your task is to summarize relevant information in 'SEARCH_OUTPUT'. \
The summary must only contain relevant information with respect to the SEARCH_QUERY. You must adhere to the following 'INSTRUCTIONS'. 

INSTRUCTIONS:
* Carefully read the 'SEARCH_QUERY'
* Select only the most relevant information from 'SEARCH_OUTPUT' that is useful and relevant and could help answering the search query
* An information can be considered relevant if it might support or refute the search query
* Examine if the information in search output relates to the search query or not
* Solely respond with "Error", if there is no relevant information in 'SEARCH_OUTPUT'.
* Summarize the most relevant information in a way that is concise and informative and include the dates mentioned
* You must not infer or add any new information, but only summarize the existing statements in an unbiased way
* If there is conflicting information, you must include both sides of the argument in the summary
* If there are dates mentioned along with the relevant information, you must include them.
* You must provide your response in the format specified under "OUTPUT_FORMAT"
* Do not include any other contents in your response.

SEARCH_QUERY:
{input_query}

SEARCH_OUTPUT:
```
{chunks}
```

OUTPUT_FORMAT:
* Only output the summary containing the most relevant information from 'SEARCH_OUTPUT' that may help to answer the search query and reveal if the event happens and on what date the event is expected to occur.
* You must not make references to the search query and its outcome, but only summarize the most relevant information from 'SEARCH_OUTPUT'.
* The summary must be structured in comprehensive bulletpoints.
* Solely respond with "Error", if there is no relevant information in 'SEARCH_OUTPUT'.
* Do not include any other contents in your response!
"""

# Global constants for possible attribute names for release and update dates
RELEASE_DATE_NAMES = [
    'date',
    'pubdate',
    'publishdate',
    'OriginalPublicationDate',
    'dateCreated',
    'article:published_time',
    'sailthru.date',
    'article.published',
    'published-date',
    'og:published_time',
    'publication_date',
    'publishedDate',
    'dc.date',
    'DC.date',
    'article:published',
    'article_date_original',
    'cXenseParse:recs:publishtime',
    'DATE_PUBLISHED',
    'pub-date',
    'datePublished',
    'date_published',
    'ArticleDate',
    'time_published',
    'article:published_date',
    'parsely-pub-date',
    'publish-date',
    'pubdatetime',
    'published_time',
    'publishedtime',
    'article_date',
    'created_date',
    'published_at',
    'lastPublishedDate',
    'og:published_time',
    'og:release_date',
    'article:published_time',
    'og:publication_date',
    'og:pubdate',
    'article:publication_date',
    'product:availability_starts',
    'product:release_date',
    'event:start_date',
    'event:release_date',
    'og:time_published',
    'og:start_date',
    'og:created',
    'og:creation_date',
    'og:launch_date',
    'og:first_published',
    'og:original_publication_date',
    'article:published',
    'article:pub_date',
    'news:published_time',
    'news:publication_date',
    'blog:published_time',
    'blog:publication_date',
    'report:published_time',
    'report:publication_date',
    'webpage:published_time',
    'webpage:publication_date',
    'post:published_time',
    'post:publication_date',
    'item:published_time',
    'item:publication_date'
]

UPDATE_DATE_NAMES = [
    'lastmod',
    'lastmodified',
    'last-modified',
    'updated',
    'dateModified',
    'article:modified_time',
    'modified_date',
    'article:modified',
    'og:updated_time',
    'mod_date',
    'modifiedDate',
    'lastModifiedDate',
    'lastUpdate',
    'last_updated',
    'LastUpdated',
    'UpdateDate',
    'updated_date',
    'revision_date',
    'sentry:revision',
    'article:modified_date',
    'date_updated',
    'time_updated',
    'lastUpdatedDate',
    'last-update-date',
    'lastupdate',
    'dateLastModified',
    'article:update_time',
    'modified_time',
    'last_modified_date',
    'date_last_modified',
    'og:updated_time',
    'og:modified_time',
    'article:modified_time',
    'og:modification_date',
    'og:mod_time',
    'article:modification_date',
    'product:availability_ends',
    'product:modified_date',
    'event:end_date',
    'event:updated_date',
    'og:time_modified',
    'og:end_date',
    'og:last_modified',
    'og:modification_date',
    'og:revision_date',
    'og:last_updated',
    'og:most_recent_update',
    'article:updated',
    'article:mod_date',
    'news:updated_time',
    'news:modification_date',
    'blog:updated_time',
    'blog:modification_date',
    'report:updated_time',
    'report:modification_date',
    'webpage:updated_time',
    'webpage:modification_date',
    'post:updated_time',
    'post:modification_date',
    'item:updated_time',
    'item:modification_date'
]

# Global constant for HTML tags to remove
HTML_TAGS_TO_REMOVE = [
    "script",
    "style",
    "header",
    "footer",
    "aside",
    "nav",
    "form",
    "button",
    "iframe",
    "input",
    "textarea",
    "select",
    "option",
    "label",
    "fieldset",
    "legend",
    "img",
    "audio",
    "video",
    "source",
    "track",
    "canvas",
    "svg",
    "object",
    "param",
    "embed",
    "link",
    ".breadcrumb",
    ".pagination",
    ".nav",
    ".ad",
    ".sidebar",
    ".popup",
    ".modal",
    ".social-icons",
    ".hamburger-menu",
]

 
class WebPage:
    _id_counter = 0
    id: int
    url: str
    html: Optional[str] = None
    scraped_text: Optional[str] = None
    publisher: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    publication_date: Optional[str] = None
    chunks_sorted: List[str] = Field(default_factory=list)
    relevant_chunks_summary: Optional[str] = None
    final_output: Optional[str] = None

    def __init__(self, url, html=None, title=None, description=None, publication_date=None, publisher=None):
        type(self)._id_counter += 1
        self.id = type(self)._id_counter
        self.url = url
        self.html = html
        self.scraped_text = None
        self.publisher = publisher
        self.title = title
        self.description = description
        self.publication_date = publication_date
        self.chunks_sorted = []
        self.extract_attribute_names = ["title", "description", "publication_date", "publisher"]


    def get_title(self, soup, scripts):
        try:
            title = soup.title
            if title:
                return title.string.strip()
        except AttributeError:
            pass

        # If the title was not found or an AttributeError was caught, attempt to find the title using meta tags.
        title = soup.find("meta", attrs={"name": "title"}) or soup.find("meta", attrs={"property": "title"})
        if title and title.get("content"):
            return title["content"].strip()

        # If no title was found return "n/a".
        return "n/a"


    def get_description(self, soup, scripts):
        description = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "description"})
        if description and description.get("content"):
            return description["content"].strip()
        return "n/a"


    def get_publisher(self, soup, scripts):
        for script in scripts:
            try:
                data = json.loads(script.string)
                publisher = self._find_publisher(data)
                return publisher
            except json.JSONDecodeError as e:
                print(f"Error decoding JSON: {e}")
                continue
            except Exception as e:
                raise Exception(f"Error extracting publisher for webpage {self.url}") from e
        
        publisher = soup.find("meta", attrs={"name": "publisher"}) or soup.find("meta", attrs={"property": "publisher"})
        
        if publisher and publisher.get("content"):
            return publisher["content"].strip()
        else:
            return "n/a"


    def get_date(self, soup, scripts):
        for script in scripts:
            try:
                data = json.loads(script.string)
                data = get_first_dict_from_list(data)
                date = find_release_date_in_data(data)
                if date:
                    return format_date(date)
            except json.JSONDecodeError as e:
                print(f"Error decoding JSON: {e}\nScript: {script}")
                continue
            except Exception as e:
                raise Exception(f"Error extracting publisher for webpage {self.url}") from e

        # If not found, then look for release or publication date
        for name in RELEASE_DATE_NAMES:
            meta_tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
            if meta_tag and meta_tag.get("content"):
                return format_date(meta_tag["content"])
        return "n/a"


    def extract_page_attributes(
        self,
    ) -> object:
        """Retrieves the release date from the soup object containing the HTML tree"""
        if self.html:
            soup = BeautifulSoup(self.html, "html.parser")
            scripts = soup.find_all('script', type='application/ld+json')
            for attribute_name in self.extract_attribute_names:
                if attribute_name == "title":
                    self.title = self.get_title(soup, scripts)
                elif attribute_name == "description":
                    self.description = self.get_description(soup, scripts)
                elif attribute_name == "publication_date":
                    self.publication_date = self.get_date(soup, scripts)
                elif attribute_name == "publisher":
                    self.publisher = self.get_publisher(soup, scripts)
                else:
                    raise ValueError(f"Invalid attribute: {attribute_name}")
        else:
            print("No HTML content to extract page attributes from.\nURL: {self.url}\nHTML: {self.html}")
        
        return self


    def to_prompt(self):
        """
        Function to convert article attributes into a structured format for LLM prompts.
        """
        page_info = f"ID: {self.id}\n"
        page_info += f"URL: {self.url}\n"
        page_info += f"Title: {self.title or 'Untitled'}\n"
        page_info += f"Description: {self.description or 'n/a'}\n"
        page_info += f"Published: {self.publication_date or 'Unknown'}\n"
        page_info += f"Publisher: {self.publisher or 'Unknown'}"
        
        return page_info
    

    def _find_publisher(self, data):
        def extract_names(item, key):
            """Helper function to extract names from a field that could be a list or a single object."""
            if isinstance(item, list):
                return [entry.get('name', 'Unknown name') for entry in item]
            elif isinstance(item, dict):
                return item.get('name', 'Unknown name')
            return 'n/a'

        # If data is a dictionary, look for 'publisher' or 'author' directly
        if isinstance(data, dict):
            if 'publisher' in data:
                return extract_names(data['publisher'], 'publisher')
            elif 'author' in data:
                return extract_names(data['author'], 'author')

        # If data is a list, iterate through it and look for 'publisher' or 'author' in each item
        elif isinstance(data, list):
            for item in data:
                if 'publisher' in item:
                    return extract_names(item['publisher'], 'publisher')
                elif 'author' in item:
                    return extract_names(item['author'], 'author')

        # If no 'publisher' or 'author' found, or data is neither a list nor a dict
        return 'n/a'


class TextChunk(BaseModel):
    text: str
    url: str
    num_tokens: Optional[int] = None
    embedding: Optional[List[float]] = None
    similarity: Optional[float] = None

    
def trim_json_formatting(output_string):
    # Regular expression pattern that matches the start and end markers with optional newline characters
    pattern = r'^\n?```\n?json\n?\s*({.*?})\n?```\n?$'
    
    # Use re.DOTALL to make '.' match newlines as well
    match = re.match(pattern, output_string, re.DOTALL)
    
    if match:
        # Extract the JSON part from the matched pattern
        print("JSON formatting characters found and removed")
        formatted_json = match.group(1)
        return formatted_json
    else:
        # Return the original string if no match is found
        return output_string


def trim_chunks_string(
    chunks_string: str,
    enc: tiktoken.Encoding,
    max_tokens: int = MAX_CHUNKS_TOKENS_TO_SUMMARIZE
) -> str:
    """Trim a string to a maximum number of tokens for summarization"""
    encoding = enc.encode(chunks_string)
    if len(encoding) > max_tokens:
        encoding = encoding[:max_tokens]
    return enc.decode(encoding)


def find_release_date_in_data(data):
    for name in RELEASE_DATE_NAMES:
        if name in data:
            return data[name]
    return None


def format_date(date_string) -> str:
    # Desired format "February 16, 2024, 3:30 PM"
    format_str = "%B %d, %Y"

    # Handle the case where the date string is "unknown"
    if date_string.lower() == "unknown":
        return date_string
    try:
        # Parse the date string to datetime object
        parsed_date = parser.parse(date_string)

        # Adjust for AM/PM format, removing leading 0 in hour for consistency
        formatted_date = parsed_date.strftime(format_str).lstrip("0").replace(" 0", " ")
        return formatted_date
    except (ValueError, TypeError):
        # If there's an error during parsing, return the original string
        return date_string
    

def parse_date_str(date_str: str) -> datetime:
    # Desired format "February 16, 2024, 3:30 PM"
    datetime_format = "%B %d, %Y"
    try:
        return datetime.strptime(date_str, datetime_format)
    except (ValueError, TypeError):
        return datetime.min
    
def remove_date_from_query(query: str) -> str:
    # Define a regex pattern to match dates
    date_pattern = r"\b( on or before | by | on )?\d{1,2} (January|February|March|April|May|June|July|August|September|October|November|December) \d{4}\b"
    new_query = re.sub(date_pattern, "", query)
    return new_query


def recursive_character_text_splitter(text, max_tokens, overlap):
    if len(text) <= max_tokens:
        return [text]
    else:
        return [text[i:i+max_tokens] for i in range(0, len(text), max_tokens - overlap)]


def count_tokens(text: str, model: str) -> int:
    """Count the number of tokens in a text."""
    enc = encoding_for_model(model)
    return len(enc.encode(text))


def get_first_dict_from_list(data):
    """Returns the first item if data is a list of dictionaries"""
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        return data[0]
    else:
        return data  # or raise an appropriate exception
    

def format_additional_information(web_pages: List[WebPage]) -> str:
    """Format the additional information from the web pages"""
    formatted_information = ""
    for i, web_page in enumerate(web_pages):
        # formatted_information += f"ARTICLE {i+1}: {web_page.title}, PUBLISHER: {web_page.publisher}, PUBLICATION_DATE: {web_page.publication_date}\n"
        formatted_information += f"ARTICLE {i+1}: PUBLISHER: {web_page.publisher}, PUBLICATION_DATE: {web_page.publication_date}\n"
        formatted_information += f"{web_page.final_output}\n\n"
    return formatted_information


def search_google(query: str, api_key: str, engine: str, num: int) -> List[str]:
    """Search Google using a custom search engine."""
    service = build("customsearch", "v1", developerKey=api_key)
    search = (
        service.cse()
        .list(
            q=query,
            cx=engine,
            num=num,
        )
        .execute()
    )
    return [result["link"] for result in search.get("items", [])]


def process_in_batches(
    web_pages: List[WebPage],
    batch_size: int = 15,
    timeout: int = 10
) -> Generator[None, None, List[Tuple[Future, WebPage]]]:
    if batch_size <= 0:
        raise ValueError("The 'batch_size' size must be greater than zero.")
    
    if timeout <= 0:
        raise ValueError("The 'timeout' must be greater than zero.")

    session = Session()
    session.max_redirects = 5

    # User-Agent headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/117.0'
    }
    session.headers.update(headers)

    with ThreadPoolExecutor() as executor:
        # Loop through the URLs in batches
        for i in range(0, len(web_pages), batch_size):
            batch = web_pages[i:i + batch_size]
            
            # Submit HEAD requests for all URLs in the batch
            head_futures = {executor.submit(session.head, web_page.url, headers=headers, timeout=timeout, allow_redirects=True): web_page for web_page in batch}
            
            # Process HEAD requests as they complete
            get_futures = []
            for future in as_completed(head_futures):
                web_page = head_futures[future]
                try:
                    head_response = future.result()
                    if 'text/html' in head_response.headers.get('Content-Type', ''):
                        # Only submit GET requests for URLs with 'text/html' Content-Type
                        get_future = executor.submit(session.get, web_page.url, headers=headers, timeout=timeout, allow_redirects=True)
                        get_futures.append((get_future, web_page))
                except requests.exceptions.Timeout:
                    print(f"HEAD request for {web_page.url} timed out.")
                except Exception as e:
                    print(f"Error processing HEAD request for {web_page.url}: {e}")

            yield get_futures


def embed_batch(client: OpenAI, batch):
    """
    Helper function to process a single batch of texts and return the embeddings.
    """
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[text_chunk.text for text_chunk in batch]
    )

    # Assert the order of documents in the response matches the request
    for i, data in enumerate(response.data):
        assert i == data.index, "Document order in the response does not match the request."

    # Return the embeddings
    return [data.embedding for data in response.data]


def sort_text_chunks(
    client: OpenAI, query: str, text_chunks_embedded: List[TextChunk]
) -> List[TextChunk]:
    """Similarity search to find similar chunks to a query"""

    print("\nINPUT QUERY:")  
    print(query)
    print()

    query_embedding = (
        client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=query,
        )
        .data[0]
        .embedding
    )

    index = faiss.IndexFlatIP(EMBEDDING_SIZE)
    index.add(np.array([text_chunk.embedding for text_chunk in text_chunks_embedded]))
    D, I = index.search(np.array([query_embedding]), len(text_chunks_embedded))
    print("SIMILAR CHUNK INDICES (SORTED IN DESCENDING ORDER):")
    print(I)
    print()
    print("SIMILARITY SCORES (SORTED IN DESCENDING ORDER):")
    print(D)
    print()
    for i, sim in enumerate(D[0]):
        text_chunks_embedded[I[0][i]].similarity = sim
    return [text_chunks_embedded[i] for i in I[0]]


def get_embeddings(client: OpenAI, text_chunks: List[TextChunk], enc: tiktoken.Encoding) -> List[TextChunk]:
    """Get embeddings for the text chunks."""
    print("Start getting embeddings ...")
    
    # Batch the text chunks that the sum of tokens is less than MAX_EMBEDDING_TOKEN_INPUT
    batches = []
    current_batch = []
    current_batch_token_count = 0
    for text_chunk in text_chunks:
        text_chunk.num_tokens  = len(enc.encode(text_chunk.text))
        if text_chunk.num_tokens + current_batch_token_count <= MAX_EMBEDDING_TOKEN_INPUT:
            # Add document to the batch if token limit is not exceeded
            current_batch.append(text_chunk)
            current_batch_token_count += text_chunk.num_tokens
        else:
            # Process the current batch and start a new one if the token limit would be exceeded
            batches.append(current_batch)
            current_batch = [text_chunk]
            current_batch_token_count = text_chunk.num_tokens

    # Add the last batch
    if current_batch:
        batches.append(current_batch)


    # Process batches in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_batch = {executor.submit(embed_batch, client, batch): batch for batch in batches}

        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                embeddings = future.result()
                # Assign embeddings to the corresponding documents
                for text_chunk, embedding in zip(batch, embeddings):
                    #print(f"Embedding for {text_chunk.text}: {embedding}")
                    text_chunk.embedding = embedding

            except Exception as e:
                print(f"Exception: {e}")
    print("Embeddings processed.")

    return text_chunks


def get_chunks(web_pages: List[WebPage]) -> List[WebPage]:
    """Create chunks from the text of all web pages"""
    text_chunks = []
    for web_page in web_pages:
        if web_page.scraped_text:
            chunks = recursive_character_text_splitter(web_page.scraped_text, TEXT_CHUNK_LENGTH, TEXT_CHUNK_OVERLAP)
            # print the first three chunks
            text_chunks.extend(TextChunk(text=chunk, url=web_page.url) for chunk in chunks)

    return text_chunks


def scrape_web_pages(web_pages: List[WebPage], week_interval, max_num_char: int = 10000) -> List[WebPage]:
    """Scrape text from web pages"""
    filtered_web_pages = []
    investigate_urls = []
    for web_page in web_pages:
        if web_page.html:
            date_parsed = parse_date_str(web_page.publication_date)
            if date_parsed > datetime.now() - timedelta(weeks=week_interval) or date_parsed == datetime.min:
                if web_page.url in investigate_urls:
                    print(web_page.html)
                # Clean the text
                doc_html2str = Document(web_page.html)
                doc_sum = doc_html2str.summary()
                h = html2text.HTML2Text()
                h.ignore_links = True
                h.ignore_images = True
                h.ignore_emphasis = True
                scraped_text = h.handle(doc_sum)
                scraped_text = "  ".join([x.strip() for x in scraped_text.split("\n")])
                scraped_text = re.sub(r'\s+', ' ', scraped_text)
                
                # If the scraped text is too short, try a manual scraping approach with BeautifulSoup
                if len(scraped_text) < 300:
                    print(f"\nScraped text for {web_page.url} has less than 300 characters: {len(scraped_text)}.")
                    print("Trying a different approach...")
                    soup = BeautifulSoup(web_page.html, "lxml")
                    
                    # Remove unnecessary tags to clean up html
                    for element in soup(HTML_TAGS_TO_REMOVE):
                        element.replace_with(NavigableString(' '))

                    text = h.handle(soup.prettify())
                    text = "  ".join([x.strip() for x in text.split("\n")])
                    text = re.sub(r'\s+', ' ', text)
                    scraped_text = text

                    if len(scraped_text) < 300 and not (web_page.title == "n/a" and web_page.description == "n/a"):
                        prefix = f"{web_page.title}. {web_page.description}."
                        scraped_text = prefix + scraped_text
                        print(f"Scraped text still less than 300 characters. Added title and description to the text.")
                    else:
                        scraped_text = None
                        print("Scraped text has still less than 300 characters and no title and description. Skipping...")
                
                if scraped_text:
                    web_page.scraped_text = scraped_text[:max_num_char]
                
                filtered_web_pages.append(web_page)

            else:
                web_page.scraped_text = None
                print(f"Publication date {web_page.publication_date} is older than {week_interval} weeks.")
        else:
            web_page.html = None
            print("HTML content is not available for web page.")

    return filtered_web_pages


def extract_html_texts(
    web_pages: List[WebPage],
) -> List[WebPage]:    
    # Initialize empty list for storing extracted sentences along with their similarity scores, release dates and urls
    parsed_web_pages = []

    # Process URLs in batches
    for batch in process_in_batches(web_pages=web_pages):
        for future, web_page in tqdm(batch, desc="Processing URLs"):
            if future is None:
                print(f"Future for {web_page.url} is None.")
                continue
            try:
                result = future.result()
                if result.status_code == 200:
                    # Extract relevant information for the event question
                    web_page.html = result.text
                    if web_page.html is None:
                        print(f"HTML content for {web_page.url} is None.")
                    parsed_web_page = web_page.extract_page_attributes()
                    parsed_web_pages.append(parsed_web_page)
                elif result.status_code != 200:
                    print(f"Request for {web_page.url} returned status code {result.status_code}.")
                elif 'text/html' not in result.headers.get('Content-Type', ''):
                    print(f"Content-Type for {web_page.url} is not 'text/html'.")
    
            except requests.exceptions.Timeout:
                print(f"Request for {web_page.url} timed out.")
            
            except Exception as e:
                print(f"An error occurred in extract_html_texts: {e}")

    print("Web pages parsed successfully.\n")

    return parsed_web_pages


def get_urls_from_queries(
    queries: List[str],
    api_key: str,
    engine: str,
    num: int = 3
) -> List[str]:
    """Fetch unique URLs from search engine queries, limiting the number of URLs per query."""
    results = set()
    max_num_fetch = 10
    omit_keywords = ["pdf", "instagram", "youtube", "reddit"]

    if num > max_num_fetch:
        raise ValueError(f"The maximum number of URLs per query is {max_num_fetch}.")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(search_google, query, api_key, engine, max_num_fetch) for query in queries}
        for future in as_completed(futures):
            result = future.result()
            count = 0
            for url in result:
                if url not in results and not any(keyword in url for keyword in omit_keywords):
                    results.add(url)
                    count += 1
                    if count >= num:
                        break
                else:
                    continue

    print("\nget_urls_from_queries result:")
    for url in results:
        print(url)

    return list(results)


def fetch_queries(
    client: OpenAI,
    input_query: str,
    engine="gpt-3.5-turbo",
    market_rules = None,
    counter_callback=None,
    temperature=1.0,
    max_attempts=2,

) -> List[str]:
    """Fetch queries from the OpenAI engine"""
    attempts = 0
    while attempts < max_attempts:
        try:
            research_plan_prompt = RESEARCH_PLAN_PROMPT_TEMPLATE.format(query=input_query, search_limit="12")
            messages = [
                {"role": "system", "content": "You are a professional researcher."},
                {"role": "user", "content": f"Get the market rules for the prediction market question {input_query}"},
                {"role": "assistant", "content": market_rules},
                {"role": "user", "content": research_plan_prompt},
            ]
            
            # Fetch queries from the OpenAI engine
            response = client.chat.completions.create(
                model=engine,
                messages=messages,
                temperature=temperature,
            )
            if counter_callback is not None:
                counter_callback(
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    model=engine,
                    token_counter=count_tokens,
                )
            search_plan = response.choices[0].message.content
            print("\nSEARCH PLAN:")
            print(search_plan)
            
            messages = [
                {"role": "system", "content": "You are a professional researcher."},
                {"role": "user", "content": research_plan_prompt},
                {"role": "assistant", "content": search_plan},
                {"role": "user", "content": QUERY_RERANKING_PROMPT_TEMPLATE},
            ]
            
            # Fetch reranked and selected queries from the OpenAI engine
            response = client.chat.completions.create(
                model=engine,
                messages=messages,
                temperature=0.0,
            )
            if counter_callback is not None:
                counter_callback(
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    model=engine,
                    token_counter=count_tokens,
                )
            output = response.choices[0].message.content

            # Parse the response content
            trimmed_output = trim_json_formatting(output)
            print(trimmed_output)
            json_data = json.loads(trimmed_output)
            queries = json_data["queries"]
            return queries, counter_callback  # Return the parsed data if successful
        except Exception as e:
            print(f"Attempt {attempts + 1} failed with error: {e}")
            attempts += 1
            if attempts == max_attempts:
                print("Maximum attempts reached, returning an empty string.")
                return []


def summarize_relevant_chunks(
        web_pages: List[WebPage],
        input_query: str,
        client: OpenAI,
        enc: tiktoken.Encoding,
        counter_callback,
        engine="gpt-3.5-turbo",
        temperature=0.0,
) -> List[WebPage]:
    def summarize_for_web_page(web_page: WebPage) -> None:
        chunks_string = ""
        if web_page.chunks_sorted:
            for chunk in web_page.chunks_sorted:
                chunks_string += f"\n…{chunk}…\n"
        else:
            web_page.relevant_chunks_summary = "Error"
            return
        
        # article_header = f"ARTICLE TITLE: {web_page.title}, PUBLISHER: {web_page.publisher}, PUBLICATION_DATE: {web_page.publication_date}\n"
        # chunks_string = article_header + chunks_string
        trimmed_chunks = trim_chunks_string(chunks_string, enc)

        input_query_no_date = remove_date_from_query(input_query)
        summarize_prompt = SUMMARIZE_PROMPT_WITH_DATE.format(input_query=input_query_no_date, chunks=trimmed_chunks)
        print()
        print(summarize_prompt)
        print()
        messages = [
            {"role": "system", "content": "You are a professional journalist and researcher."},
            {"role": "user", "content": summarize_prompt},
        ]
        response = client.chat.completions.create(
            model=engine,
            messages=messages,
            temperature=temperature,
        )
        if counter_callback is not None:
            counter_callback(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                model=engine,
                token_counter=count_tokens,
            )
        output = response.choices[0].message.content
        print("\nSUMMARIZE RELEVANT CHUNKS OUTPUT:")
        print(output)
        print()

        web_page.relevant_chunks_summary = output

    with ThreadPoolExecutor() as executor:
        # Submit all web pages to the executor for processing
        future_to_web_page = {executor.submit(summarize_for_web_page, web_page): web_page for web_page in web_pages}

        # Wait for all futures to complete
        for future in as_completed(future_to_web_page):
            web_page = future_to_web_page[future]
            try:
                # Result is None, as web_page objects are modified in-place
                future.result()
            except Exception as e:
                print(f'Web page {web_page.url} generated an exception: {e}')
    web_pages = [web_page for web_page in web_pages if "Error" not in web_page.relevant_chunks_summary]
    return web_pages, counter_callback


def summarize_over_summarized_chunks(
    web_pages: List[WebPage],
    input_query: str,
    client: OpenAI,
    counter_callback,
    engine="gpt-3.5-turbo",
    temperature=0.0,
) -> List[WebPage]:
    
    # Add WebPage ID after each line in relevant_chunks_summary
    # Initialize an empty list to hold all modified lines
    all_lines_with_id = []

    for web_page in web_pages:
        if web_page.relevant_chunks_summary:
            # Split the summary into lines
            lines = web_page.relevant_chunks_summary.split('\n')
            
            # Append the web page ID to each line and add it to the list
            all_lines_with_id.extend([line + f" ({web_page.id})" for line in lines if line.strip() != ''])

    # Join all modified lines into a single string
    all_relevant_chunks_summary = '\n'.join(all_lines_with_id)

    prompt = PREVIOUS_SUMMARY_PROMPT.format(input_query=input_query, chunks=all_relevant_chunks_summary)

    print(f"\nPREVIOUS SUMMARY PROMPT:")
    print(prompt)
    print()

    messages = [
        {"role": "system", "content": "You are a professional journalist."},
        {"role": "user", "content": prompt},
    ]
    response = client.chat.completions.create(
        model=engine,
        messages=messages,
        temperature=temperature,
    )
    if counter_callback is not None:
        counter_callback(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            model=engine,
            token_counter=count_tokens,
        )
    output = response.choices[0].message.content
    print("\nSUMMARIZE OVER SUMMARIZED CHUNKS OUTPUT:")
    print(output)
    
    # Split the combined string into individual lines
    lines = output.strip().split('\n')

    # Mapping of web_page.id to web_page object for quick access
    web_pages_dict = {str(web_page.id): web_page for web_page in web_pages}

    # Initialize a set to track modified web_page IDs
    modified_ids = set()

    # Process each line to extract the web_page.id and content, then update the relevant web page
    for line in lines:
        match = re.match(r"^(.*) \((\d+)\)$", line)
        if match:
            content, web_page_id = match.groups()
            # Check if this web_page_id is in our dictionary of web pages
            if web_page_id in web_pages_dict:
                # Update the relevant_chunks_summary of the corresponding web page
                web_page = web_pages_dict[web_page_id]
                if web_page.final_output:
                    web_page.final_output += '\n' + content
                else:
                    web_page.final_output = content
                # Mark this ID as modified
                modified_ids.add(web_page_id)

    # Collect modified web_page objects based on modified_ids
    modified_web_pages = [web_pages_dict[web_page_id] for web_page_id in modified_ids]

    return modified_web_pages, counter_callback


# def summarize_chunks_bulletpoints(
#     web_pages: List[WebPage],
#     input_query: str,
#     client: OpenAI,
#     enc: tiktoken.Encoding,
#     counter_callback,
#     engine="gpt-3.5-turbo",
#     temperature=0.0,
# ) -> List[WebPage]:
#     """Summarize the relevant bullet points into a paragraph."""
#     for web_page in web_pages:
#         if web_page.relevant_chunks_summary:
#             prompt

#     SUMMARIZE_CHUNKS_BULLETPOINTS
#     return web_pages, counter_callback


def research(
    market_question: str,
    client: OpenAI,
    google_api_key: str,
    google_engine_id: str,
    engine: str,
    market_rules: str,
    counter_callback,
    num_urls: int = NUM_URLS_PER_QUERY,
):
    """Research additional information based on a prediction market question"""
    """Research additional information based on a prediction market question"""
    
    """Research additional information based on a prediction market question"""    
    
    # Generate a list of sub-queries
    queries, counter_callback = fetch_queries(client, market_question, engine, market_rules, counter_callback)
    
    # Get URLs from sub-queries
    urls = get_urls_from_queries(
        queries,
        api_key=google_api_key,
        engine=google_engine_id,
        num=num_urls,
    )
    web_pages = [WebPage(url) for url in urls]
    web_pages = extract_html_texts(web_pages)

    week_interval = WEEKS_TO_SCRAPE_NEWS
    # Scrape text from web pages not older <week_interval> weeks
    web_pages = scrape_web_pages(web_pages, week_interval)

    for page in web_pages:
        if page.scraped_text:
            print(f"\n{page.to_prompt()}")
            print(f"Length of scraped text: {len(page.scraped_text)}\n")

    text_chunks = get_chunks(web_pages)
    
    # Initialize Tokenizer
    enc = tiktoken.get_encoding("cl100k_base") 
    
    text_chunks_embedded = get_embeddings(client, text_chunks, enc) if text_chunks else []
    text_chunks_sorted = sort_text_chunks(client, market_question, text_chunks_embedded) if text_chunks_embedded else []
    text_chunks_limited = text_chunks_sorted[:MAX_TEXT_CHUNKS_TOTAL]
    # print("\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>\n")
    # for chunk in text_chunks_sorted:
    #     print(f"Similarity: {chunk.similarity}")
    #     print(chunk.text)
    #     print()

    # Create a dictionary mapping URLs to WebPage objects for quicker lookups
    web_pages_dict = {web_page.url: web_page for web_page in web_pages}

    for text_chunk in text_chunks_limited:
        if text_chunk.url in web_pages_dict:
            web_pages_dict[text_chunk.url].chunks_sorted.append(text_chunk.text)

    web_pages = list(web_pages_dict.values())
    web_pages, counter_callback = summarize_relevant_chunks(web_pages, market_question, client, enc, counter_callback)
    web_pages = sorted(web_pages, key=lambda web_page: parse_date_str(web_page.publication_date))

    #web_pages, counter_callback = summarize_chunks_bulletpoints(web_pages, market_question, client, enc, counter_callback)
    web_pages, counter_callback = summarize_over_summarized_chunks(web_pages, market_question, client, counter_callback)
    web_pages = sorted(web_pages, key=lambda web_page: parse_date_str(web_page.publication_date))


    additional_information = format_additional_information(web_pages)
    if additional_information:
        additional_information += (
            f"Disclaimer: This search output was retrieved on {datetime.now().strftime('%B %d, %Y')} and does not claim to be exhaustive or definitive."
        )

    return additional_information, counter_callback
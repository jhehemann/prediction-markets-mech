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

"""This module implements a Mech tool for binary predictions."""

from datetime import datetime
import json
import re
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple, Callable
from packages.jhehemann.customs.infer_market_rules.infer_market_rules import get_market_rules
from packages.jhehemann.customs.research.research import research

from openai import OpenAI

import spacy
from spacy import Language
from spacy.cli import download
from spacy.tokens import Span
from tiktoken import encoding_for_model


client: Optional[OpenAI] = None

class OpenAIClientManager:
    """Client context manager for OpenAI."""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def __enter__(self) -> OpenAI:
        global client
        if client is None:
            client = OpenAI(api_key=self.api_key)
        return client

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        global client
        if client is not None:
            client.close()
            client = None

def count_tokens(text: str, model: str) -> int:
    """Count the number of tokens in a text."""
    enc = encoding_for_model(model)
    return len(enc.encode(text))

FrequenciesType = Dict[str, float]
ScoresType = Dict[Span, float]

DEFAULT_OPENAI_SETTINGS = {
    "max_tokens": 500,
    "temperature": 0.0,
}
ALLOWED_TOOLS = [
    "prediction-with-rules-and-report"
]
MAX_TOKENS = {
    "gpt-3.5-turbo": 4096,
    "gpt-4": 8192,
}
TOOL_TO_ENGINE = {tool: "gpt-3.5-turbo" for tool in ALLOWED_TOOLS}
# the default number of URLs to fetch online information for
DEFAULT_NUM_URLS = defaultdict(lambda: 3)
DEFAULT_NUM_URLS["prediction-with-rules-and-report"] = 3
# the default number of words to fetch online information for
DEFAULT_NUM_WORDS: Dict[str, Optional[int]] = defaultdict(lambda: 300)

# how much of the initial content will be kept during summarization
DEFAULT_COMPRESSION_FACTOR = 0.05
# the vocabulary to use for the summarization
DEFAULT_VOCAB = "en_core_web_sm"


REPORT_PROMPT = """
Your task is to write a concise evaluation report that discusses the potential outcome of the QUESTION found below. Your evaluation must be based \
on the SEARCH_OUTPUT and your domain expertise.
Adhere to the following instructions:

INSTRUCTIONS:
* Carefully read the QUESTION
* Analyze the SEARCH_OUTPUT and evaluate the date when the event will actually happen
* Source your domain expertise
* Give your response in the format specified under "OUTPUT_FORMAT"

OUTPUT_FORMAT:
* Introduction and Context
* Findings and Analysis (Use domain expertise to justify your answers)
    * Event (Will the exact event specified in the QUESTION happen? Has it already happened?)
    * Date (On what date will the event specified in the QUESTION happen? You must provide a specific date on what you believe the event will happen. If you are uncertain, provide a range of dates.)


QUESTION:
```
{market_question}
```

QUESTION_STATUS:
```
{question_status}
```

SEARCH_OUTPUT:
```
{additional_information}
```

Output only the report without any additional information or formatting.
"""

# OUTPUT_FORMAT "* QUESTION"
# * Conclusion with common sense reasoning
# * Caveats

REPORT_PROMPT_OLDER_BUT_MAYBE_BETTER = """
Your task is to prepare a concise and informative evaluation report that discusses the potential outcome of the QUESTION found below. Your evaluation must be based \
on the SEARCH_OUTPUT and your domain expertise.
Adhere to the following instructions:

INSTRUCTIONS:
* Carefully read the QUESTION
* Separate the QUESTION into its components
* Carefully read the search output provided.
* Analyze the search output and evaluate the date when the event will actually happen
* Source your domain expertise to provide caveats
* Give your response in the format specified under "OUTPUT_FORMAT"

SEARCH_OUTPUT:
```
{additional_information}
```

QUESTION:
```
{market_question}
```

TODAYS_DATE: {current_date}

OUTPUT_FORMAT:
* Introduction and Context
* Findings and Analysis
    - Will the event specified in the question happen?
    - On what date will the event actually happen? Has the event already happened? You must provide a specific date. If you are uncertain, provide a range of dates. Use domain expertise to justify your answer.
* Conclusion with common sense reasoning
Output only the report without any additional information or formatting.
"""

SME_GENERATION_MARKET_PROMPT = """
task question: "{question}"
"""

SME_GENERATION_SYSTEM_PROMPT = """
This task requires answering Yes or No to a specific question related to certain knowledge domains. The final opinion to the question should be determined by one or more subject matter experts (SME) of the related domains. You need to generate one or more SME roles and their role introduction that you believe to be helpful in forming a correct answer to question in the task.

Examples:
task question: "Will Apple release iphone 15 by 1 October 2023?"
[
        {
            "sme": "Technology Analyst",
            "sme_introduction": "You are a seasoned technology analyst AI assistant. Your goal is to do comprehensive research on the news on the tech companies and answer investor's interested questions in a trustful and accurate way."
        }
]
---
task question: "Will the newly elected ceremonial president of Singapore face any political scandals by 13 September 2023?"
[
        { 
            "sme":  "Political Commentator",
            "sme_introduction": "You are an experienced political commentator in Asia. Your main objective is to produce comprehensive, insightful and impartial analysis based on the relevant political news and your politic expertise to form an answer to the question releted to a political event or politician."
        }
]
---
task question: "Will the air strike conflict in Sudan be resolved by 13 September 2023?"
[
       {
            "sme:  "Military Expert",
            "sme_introduction": "You are an experienced expert in military operation and industry. Your main goal is to faithfully and accurately answer a military related question based on the provided intelligence and your professional experience"
        },
       {
            "sme:  "Diplomat",
            "sme_introduction": "You are an senior deplomat who engages in diplomacy to foster peaceful relations, negotiate agreements, and navigate complex political, economic, and social landscapes. You need to form an opinion on a question related to international conflicts based on the related information and your understading in geopolitics."
        },
]
"""


PREDICTION_PROMPT_TEMPLATE_TRY = """
You are an expert data analyst. Your task is to write a detailed evaluation and make probability estimations for the outcomes 'Yes' and 'No' of a prediction market question.
You must adhere to the following instructions:

INSTRUCTIONS:
* You are provided with the market question and the market rules that define the market question's resolution 'Yes' and 'No' under the label "USER_PROMPT". 
* You are provided with additional information from an online search under the label "ADDITIONAL_INFORMATION" that contains additional information and analysis if and when the event specified in the market question could happen.
* Take into account that today's date is {current_date}
* Write an evaluation paragraph that addresses the following points:
    - If there is a conflict between the event dates in the ADDITIONAL_INFORMATION and the deadline in the USER_PROMPT, prioritize assessing the likelihood of meeting the market question's specified deadline.
    - Use the market rules to evaluate the likelihood of the market resolving as 'Yes' and 'No'.
    - Use your domain expertise and justify your answer
* Make probability estimations for the market's outcomes 'Yes' and 'No' taking the market rules into account
* Provide your confidence in the estimation and the utility of the information in the ADDITIONAL_INFORMATION
* Give your response in the format specified under "OUTPUT_FORMAT"

ADDITIONAL_INFORMATION:
```
{report}
```

OUTPUT_FORMAT:
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()"
* The JSON must contain five fields: "market_resolution_evaluation", "p_yes", "p_no", "confidence", "info_utility" each ranging from 0 to 1, except "market_resolution_evaluation" which is a string
    - "market_resolution_evaluation": Evaluation paragraph
    - "p_yes"
    - "p_no"
    - "confidence"
    - "info_utility"
* Include only the JSON object in your output

USER_PROMPT:
```
Market question:
{market_question}

market rules:
{market_rules_part}
```
"""

PREDICTION_PROMPT_TEMPLATE_TRY = """
You are an expert data analyst. Your task is to write a final evaluation and estimate the probability of the event in the question under USER_PROMPT occurring by definition of the guidelines.

INSTRUCTIONS:
* You are provided with the input question about the event under the label "USER_PROMPT". 
* You are provided with research output from a colleague as to whether and when the event might occur based on online research under the label "RESEARCH_OUTPUT" delimited by three backticks.
* You are also provided with guidelines that help you decide whether the answer to the user question is yes or no under the label "GUIDELINES" delimited by three backticks.
* You should show your process of thinking through the problem step by step, taking the additional information into consideration, and explain your reasoning for your decision as to whether an event will occur taking into account the guidelines.
* Try to be concise in your reasoning, providing only information that is important for making a decision
* Take into account that today's date is {current_date}
* Provide your confidence in the estimation and the utility of the information in the SEARCH_OUTPUT
* Give your response in the format specified under "OUTPUT_FORMAT"

OUTPUT_FORMAT:
* Output your response as a JSON object containing five fields: "final_evaluation", "p_yes", "p_no", "confidence", "info_utility" each ranging from 0 to 1, except "final_evaluation" which is a string
    - "final_evaluation": Evaluation paragraph (chose which condition is fulfilled from the guidelines) - aim for a response of about 100 words
    - "p_yes"
    - "p_no"
    - "confidence"
    - "info_utility"
* Include only the JSON object in your output

USER_PROMPT:
```
{market_question}
```

RESEARCH_OUTPUT:
```
{report}
```

DECISION_GUIDELINES:
```
{market_rules}
```
"""

PREDICTION_PROMPT_TEMPLATE = """
You are an expert data analyst. Your task is to write a detailed evaluation and make probability estimations for the market resolving as 'Yes'.
You must adhere to the following instructions:

INSTRUCTIONS:
* You are provided with the prediction market question under the label "USER_PROMPT" consisting of an event and a specific date.
* Treat both the event and the date as uncertain. You find the truth about the event and the date in the SEARCH_OUTPUT.
* You are provided with a search output from an online search under the label "SEARCH_OUTPUT" that contains additional information and analysis if and when the event specified in the market question could happen.
* You are provided with the market rules that define the conditions for the resolution of the market under the label "MARKET_RULES".
* Take into account that today's date is {current_date}
* Imagine you have a machine that outputs the truth about the prediction market. This machine is the market rules. The machine can receive input in form of the SEARCH_OUTPUT. The machine then uses its definedd rules for the market to output the resolution of the market as 'Yes' or 'No'.
* Write an evaluation paragraph that addresses the hidden process inside the machine
* Make probability estimations for the market's outcome being 'Yes' taking the market rules and the SEARCH_OUTPUT into account
* Provide your confidence in the estimation and the utility of the information in the SEARCH_OUTPUT
* Give your response in the format specified under "OUTPUT_FORMAT"

USER_PROMPT:
```
{market_question}
```

SEARCH_OUTPUT:
```
{report}
```

OUTPUT_FORMAT:
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()"
* The JSON must contain five fields: "market_resolution_evaluation", "p_yes", "p_no", "confidence", "info_utility" each ranging from 0 to 1, except "market_resolution_evaluation" which is a string
    - "market_resolution_evaluation": Evaluation paragraph where the market resolving as 'Yes' is assessed according to the market rules. Start with the deduction and conclude with the likelihood evaluation in the end. Specifically mention dates and try not to replace them with terms like 'specified date' or similar (about 100 words)
    - "p_yes": Probability of the market resolving as 'Yes' according to the market rules
    - "p_no": Probability of the market resolving as 'No' according to the market rules
    - "confidence": Your confidence in the estimation
    - "info_utility": Utility of the information in the SEARCH_OUTPUT
* Include only the JSON object in your output

Show your process of thinking through the problem step by step.
"""
# MARKET_RULES:
# ```
# {market_rules}
# ```
# - Make a comprehensive analysis of the dates: today's date, questioned date (market question), actual event date (from the SEARCH_OUTPUT)
#     - Use the market rules to evaluate the likelihood of the market resolving as 'Yes' by referring to the SEARCH_OUTPUT.
#     - Use your domain expertise and justify your answer

PREDICTION_PROMPT_TEMPLATE_TRY = """
You are a detective and an expert in solving complicated problems with logical conclusions. Your task is to provide a logical reasoning and make probability estimations for a prediction market resolving as 'Yes' or 'No'.

INSTRUCTIONS:
* You are provided with a market question under the label "USER_PROMPT".
* This market question consists of an event and a specific date. When evaluating the resolution of the market, both the event and the date must meet the conditions specified in the market rules.
* You are provided with the market rules that define the conditions for the resolution of the market under the label "MARKET_RULES".

OUTPUT_FORMAT:
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()"
* The JSON must contain five fields: "market_resolution_evaluation", "p_yes", "p_no", "confidence", "info_utility" each ranging from 0 to 1, except "market_resolution_evaluation" which is a string
    - "market_resolution_evaluation": Evaluation paragraph where the search output is analyzed and the likelihood of the market resolving as 'Yes' is assessed according to the market rules (about 100 words)
    - "p_yes": Probability of the market resolving as 'Yes' according to the market rules
    - "p_no": Probability of the market resolving as 'No' according to the market rules
    - "confidence": Your confidence in the estimation
    - "info_utility": Utility of the information in the SEARCH_OUTPUT
* Include only the JSON object in your output

SIDE_INFORMATION:
```
{report}
```

MARKET_RULES:
```
{market_rules}
```

USER_PROMPT:
```
{market_question}
```
"""

# REASONING_PROMPT_BY = """
# You are an expert fact checker that takes in a question asking whether an event will happen on or before a given date. 
# Your role is to determine whether the event will happen before the date.

# INSTRUCTIONS
# * You are provided with the input question about the event under the label "USER_PROMPT" delimited by three backticks, which is a question about whether an event will happen before a certain date.
# * You need to determine whether the event will or will not happen. There are only two possible answers: either the event will happen or it will not happen.
# * You are provided an itemized list of information under the label "ADDITIONAL_INFORMATION" delimited by three backticks, with format "ARTICLE (N), URL: (URL), CONTENT: (CONTENT)"
# * Ideally, these will be news articles about the event in question.
# * If an item in "ADDITIONAL_INFORMATION" is not relevant, you must ignore that item for the estimation.
# * You are also provided with guidelines you must follow for making your decision under the label "GUIDELINES" delimited by three backticks.
# * You must take the information from the articles and evaluate them by the guidelines to make your decision.
# * You should show your process of thinking through the problem step by step, taking the information of the various articles into consideration, and explain your reasoning for your decision as to whether an event will occur by the specified date. 
# * The articles will not contain all the information needed to determine the answer. In this case, you may need to make an educated guess based on certain assumptions. If you need to do this, please provide your assumptions in your explanation.
# * Try to be concise in your reasoning, providing only information that is important for making a decision (aim for a response of about 200 words)
# * Do not repeat the task or instructions in the response

# USER_PROMPT:
# ```
# {user_prompt}
# ```

# ADDITIONAL_INFORMATION:
# ```
# {formatted_docs}
# ```

# GUIDELINES:
# ```
# {market_rules}
# ```
# """


PREDICTION_PROMPT = """
INSTRUCTIONS
* You are an expert data analyst. 
* You are provided with the input question about the event under the label "USER_PROMPT". 
* You are provided with a colleague's reasoning as to whether the event will occur based on online research under the label "REASONING" delimited by three backticks.
* Your task is to predict the probability of the event in the USER_PROMPT occurring.
* The answer that you give should match the answer that you come to in the reasoning field
* Give your response in the format specified under "OUTPUT_FORMAT"

USER_PROMPT:
```
{user_prompt}
```

REASONING:
```
{reasoning}
```

OUTPUT_FORMAT:
* Your output response must be only a single JSON object to be parsed by Python's "json.loads()"
* The JSON must contain five fields: "p_yes", "p_no", "confidence", "info_utility" each ranging from 0 to 1
    - "p_yes"
    - "p_no"
    - "confidence"
    - "info_utility"
* Include only the JSON object in your output
"""


# * Give your response in the format specified under "OUTPUT_FORMAT"

# OUTPUT_FORMAT:
# * Your output response must be only a single JSON object to be parsed by Python's "json.loads()"
# * The JSON must contain five fields: "market_resolution_evaluation", "p_yes", "p_no", "confidence", "info_utility" each ranging from 0 to 1, except "market_resolution_evaluation" which is a string
#     - "market_resolution_evaluation": Evaluation paragraph
#     - "p_yes"
#     - "p_no"
#     - "confidence"
#     - "info_utility"
# * Include only the JSON object in your output

def trim_json_formatting(text) -> str:
    """Trim the JSON formatting characters from string."""
    # Regex pattern that matches the start and end markers with optional newline characters
    pattern = r'^\s*```\s*json\s*({.*?})\s*```\s*$'

    # Use re.DOTALL to make '.' match newlines as well
    match = re.match(pattern, text, re.DOTALL)
    if match:
        formatted_json = match.group(1)
        return formatted_json
    else:
        return text


def remove_unwanted_fields(json_str) -> str:
    """Remove all fields from a JSON string except 'p_yes', 'p_no', 'confidence', and 'info_utility'."""
    # Load the JSON string into a Python dictionary
    data = json.loads(json_str)
    
    # Define the keys that you want to keep
    keys_to_keep = {'p_yes', 'p_no', 'confidence', 'info_utility'}
    
    # Use dictionary comprehension to keep only the desired keys
    filtered_data = {k: v for k, v in data.items() if k in keys_to_keep}
    
    # Convert the filtered dictionary back into a JSON string
    modified_json_str = json.dumps(filtered_data, indent=4)
    
    return modified_json_str


def extract_question(text) -> str:
    """Extract the question from prompt enclosed in escaped quotation marks."""
    pattern = r'\"(.*?)\"'
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def remove_date_from_query(query: str) -> str:
    """Remove time-related information from query"""
    date_pattern = r"\b(?:on or by |on or before |before |by |on )?(?:(\d{1,2})(st|nd|rd|th)? (January|February|March|April|May|June|July|August|September|October|November|December)|(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})(st|nd|rd|th)?,?) \d{4}\b"
    new_query = re.sub(date_pattern, "", query)
    return new_query


def split_before_evaluation(text) -> Tuple[str, str]:
    """Split string at last occurrence of 'Evaluation'"""
    eval_index = text.rfind("Evaluation")
    if eval_index == -1:
        return text, ""
    
    # Find the last newline character before "Evaluation"
    newline_index = text.rfind("\n", 0, eval_index)
    if newline_index == -1:
        return text, ""
    
    # Split the string at the found newline index
    part1 = text[:newline_index]
    part2 = text[newline_index + 1:]
    
    return part1, part2


def get_sme_role(
    engine, temperature, max_tokens, prompt, counter_callback=None
) -> Tuple[str, str, Optional[Callable]]:
    """Get SME title and introduction"""
    market_question = SME_GENERATION_MARKET_PROMPT.format(question=prompt)
    system_prompt = SME_GENERATION_SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": market_question},
    ]
    response = client.chat.completions.create(
        model=engine,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        n=1,
        timeout=150,
        stop=None,
    )
    generated_sme_roles = response.choices[0].message.content
    sme = json.loads(generated_sme_roles)[0]
    if counter_callback is not None:
        counter_callback(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            model=engine,
            token_counter=count_tokens,
        )
        return sme["sme"], sme["sme_introduction"], counter_callback
    return sme["sme"], sme["sme_introduction"], None


def run(**kwargs) -> Tuple[Optional[str], Any, Optional[Dict[str, Any]], Any]:
    """Run the task"""
    with OpenAIClientManager(kwargs["api_keys"]["openai"]):
        tool = kwargs["tool"]
        prompt = kwargs["prompt"]
        max_tokens = kwargs.get("max_tokens", DEFAULT_OPENAI_SETTINGS["max_tokens"])
        temperature = kwargs.get("temperature", DEFAULT_OPENAI_SETTINGS["temperature"])
        num_urls = kwargs.get("num_urls", DEFAULT_NUM_URLS[tool])
        num_words = kwargs.get("num_words", DEFAULT_NUM_WORDS[tool])
        compression_factor = kwargs.get("compression_factor", DEFAULT_COMPRESSION_FACTOR)
        vocab = kwargs.get("vocab", DEFAULT_VOCAB)
        counter_callback = kwargs.get("counter_callback", None)
        api_keys = kwargs.get("api_keys", {})
        google_api_key = api_keys.get("google_api_key", None)
        google_engine_id = api_keys.get("google_engine_id", None)

        if tool not in ALLOWED_TOOLS:
            raise ValueError(f"Tool {tool} is not supported.")

        engine = TOOL_TO_ENGINE[tool]

        # Extract the market question from the prompt delimited by escaped quotation marks
        market_question = extract_question(prompt)
        if not market_question:
            return "Market question not found in prompt", None, None, None
        print(f"MARKET QUESTION:\n{market_question}\n")

        # Get the market rules from the Infer Rules tool
        market_status, market_rules, counter_callback = get_market_rules(market_question, client, counter_callback)
        print(f"MARKET STATUS: {market_status}\n")
        print(f"MARKET RULES:\n{market_rules}\n")
        
        # Get additional information from the Research tool
        additional_inforamtion, counter_callback = research(market_question, client, google_api_key, google_engine_id, engine, market_status, market_rules, counter_callback)

        market_question_no_date = remove_date_from_query(market_question)
        market_question_when = f"When {market_question_no_date}"

        # Generate a report prompt based on the market question, market rules, additional information and the current date
        current_date = datetime.now().strftime('%B %d, %Y')
        report_prompt = REPORT_PROMPT.format(
            market_question=market_question_no_date,
            market_rules=market_rules,
            additional_information=additional_inforamtion,
            current_date=current_date,
            question_status=market_status
        )
        print(f"REPORT PROMPT:\n{report_prompt}\n")
        
        # Get the subject matter expert role and introduction
        sme = ""
        sme_introduction = ""
        try:
            sme, sme_introduction, counter_callback = get_sme_role(
                engine,
                temperature,
                max_tokens,
                market_question,
                counter_callback=counter_callback,
            )
        except Exception as e:
            print(f"An error occurred during SME role creation: {e}")
            print("Using default SME introduction.")
            sme_introduction = "You are a professional journalist."
        
        if sme:
            print(f"SME ROLE: {sme}")
        else:
            print("SME role not found.")
        print(f"SME INTRODUCTION: {sme_introduction}")
        print()

        messages_report = [
            {"role": "system", "content": sme_introduction},
            {"role": "user", "content": report_prompt},
        ]
        # Generate a report based on the messages
        response = client.chat.completions.create(
            model=engine,
            messages=messages_report,
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
        print(f"OUTPUT:\n{output}\n")
        
        prediction_prompt = PREDICTION_PROMPT_TEMPLATE.format(market_question=market_question, market_rules=market_rules, current_date=current_date, report=output)
        print(f"PREDICTION PROMPT:{prediction_prompt}")

        # system_prediction_prompt = "You are a seasoned market analyst with a deep understanding of prediction markets and consider the factors that influence their outcomes. Your goal is to provide a well-reasoned analysis based on data, trends, and expert knowledge to help individuals make informed decisions when betting on prediction market outcomes."
        system_prediction_prompt = "You are a seasoned prediction market analyst with a deep understanding of how prediction markets work and how to assess the likelihood of different market resolutions. Your goal is to provide a well-reasoned analysis and probability estimations for the resolution of the prediction market based on your expertise in prediction markets and relevant domain knowledge. Carefully consider the market rules to make your evaluation."

        messages_prediction = [
            {"role": "system", "content": system_prediction_prompt},
            {"role": "user", "content": prediction_prompt},
        ]

        thread_history = [
            {"role": "user", "content": report_prompt},
            {"role": "assistant", "content": output},
            {"role": "user", "content": prediction_prompt},
        ]
        thread_history_string = json.dumps(thread_history, indent=4)

        # Generate a prediction based on the messages
        response = client.chat.completions.create(
            model=engine,
            response_format={ "type": "json_object" },
            messages=messages_prediction,
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
        output = trim_json_formatting(output)
        print(f"OUTPUT:\n{output}\n")

        # Remove conclusion field from the JSON string
        output = remove_unwanted_fields(output)
        
        return output, thread_history_string, None, counter_callback
              

                





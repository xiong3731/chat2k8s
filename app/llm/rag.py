import pickle
import requests
import os
import logging
from typing import List, Dict, Any, Optional

from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_milvus import Milvus, BM25BuiltInFunction # 引入原生 BM25 函数
from langchain_classic.storage import InMemoryStore
from langchain_classic.chains.retrieval import create_retrieval_chain
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.retrievers import MultiQueryRetriever
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_core.documents import Document

from app.core.config import settings

logger = logging.getLogger(__name__)

class RAGService:
    def __init__(self):
        self.embeddings = OpenAIEmbeddings(
            model=settings.RAG_EMBEDDING_MODEL,
            openai_api_key=settings.RAG_EMBEDDING_API_KEY,
            openai_api_base=settings.RAG_EMBEDDING_API_BASE
        )
        # Check if RAG_LLM_API_KEY is set, otherwise use OPENAI_API_KEY or handle error
        api_key = settings.RAG_LLM_API_KEY if settings.RAG_LLM_API_KEY else settings.OPENAI_API_KEY
        
        self.llm = ChatOpenAI(
            model=settings.RAG_LLM_MODEL,
            openai_api_key=api_key,
            openai_api_base=settings.RAG_LLM_API_BASE
        )
        self.store = InMemoryStore()
        self.vectorstore = None
        self.multi_query_retriever = None
        self.rag_chain = None
        self.initialized = False
        self.store_path = "./doc_path/parent_store.pkl"

    def initialize(self):
        """Initializes the RAG service: loads parent store, connects to Milvus, sets up chain."""
        if self.initialized:
            return

        # Load parent store
        if os.path.exists(self.store_path):
            try:
                logger.info(f"Loading parent store from {self.store_path}...")
                with open(self.store_path, 'rb') as f:
                    store_data = pickle.load(f)
                    self.store = InMemoryStore()
                    self.store.mset(list(store_data.items()))
                logger.info(f"Loaded {len(store_data)} parent documents.")
            except Exception as e:
                logger.error(f"Failed to load parent store: {e}")
                self.store = InMemoryStore()
        else:
            logger.warning(f"Parent store not found at {self.store_path}. RAG might not function correctly.")
            self.store = InMemoryStore()

        # Connect to Milvus (without drop_old=True)
        try:
            self.vectorstore = Milvus(
                embedding_function=self.embeddings,
                builtin_function=BM25BuiltInFunction(output_field_names="sparse_vector"),
                connection_args={"host": settings.RAG_MILVUS_HOST, "port": settings.RAG_MILVUS_PORT},
                collection_name=settings.RAG_MILVUS_COLLECTION_NAME,
                vector_field=["dense_vector", "sparse_vector"],
                auto_id=True  # Ensure auto_id matches creation if needed, usually Milvus handles it
            )
        except Exception as e:
             logger.error(f"Failed to connect to Milvus: {e}")
             return

        # Retrieval & Rerank Logic
        hybrid_retriever = self.vectorstore.as_retriever(search_kwargs={"k": 5, "search_params": {"ans_type": "hybrid"}})
        self.multi_query_retriever = MultiQueryRetriever.from_llm(retriever=hybrid_retriever, llm=self.llm)

        # Build RAG Chain
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Use the following pieces of context to answer the user's question. If you don't know the answer, just say that you don't know, don't try to make up an answer.\n\n{context}"),
            ("human", "{input}"),
        ])

        self.rag_chain = create_retrieval_chain(RunnableLambda(self.retrieve_parent_docs), create_stuff_documents_chain(self.llm, prompt))
        self.initialized = True
        logger.info("RAG Service initialized successfully.")

    def rerank_documents(self, query, documents, top_n=3):
        if not documents: return []
        try:
            payload = {"model": settings.RAG_RERANK_MODEL, "query": query, "documents": [d.page_content for d in documents]}
            resp = requests.post(settings.RAG_RERANK_API_URL, json=payload, timeout=settings.RAG_RERANK_TIMEOUT).json()
            ranked = sorted([(documents[r["index"]], r["relevance_score"]) for r in resp.get("results", [])], key=lambda x: x[1], reverse=True)
            return [item[0] for item in ranked[:top_n]]
        except Exception as e:
            logger.error(f"Rerank failed: {e}")
            return documents[:top_n]

    def retrieve_parent_docs(self, input_dict: Dict[str, Any]) -> List[Document]:
        """Retrieve parent documents based on query."""
        query = input_dict.get("input", "")
        if not query or not self.multi_query_retriever:
            return []
            
        try:
            # 1. Retrieve child chunks
            sub_docs = self.multi_query_retriever.invoke(query)
            if not sub_docs:
                return []
                
            # 2. Rerank
            reranked_docs = self.rerank_documents(query, sub_docs)
            
            # 3. Get parent docs
            parent_docs = []
            parent_ids = set()
            
            for doc in reranked_docs:
                # Try to get parent doc ID from metadata
                # Common keys: doc_id, parent_id, or source
                doc_id = doc.metadata.get("doc_id") or doc.metadata.get("parent_id")
                
                if doc_id and doc_id not in parent_ids:
                    # Retrieve from store
                    try:
                        parents = self.store.mget([doc_id])
                        if parents and parents[0]:
                            parent_docs.append(parents[0])
                            parent_ids.add(doc_id)
                        else:
                            # Fallback if not found in store
                            parent_docs.append(doc)
                    except Exception:
                        parent_docs.append(doc)
                elif not doc_id:
                    # No ID, just use the doc
                    parent_docs.append(doc)
            
            return parent_docs
        except Exception as e:
            logger.error(f"Error in retrieve_parent_docs: {e}")
            return []

    async def aquery(self, query: str) -> str:
        """Async query the RAG chain."""
        if not self.initialized:
            self.initialize()
            
        if not self.rag_chain:
            return "RAG Service not initialized or failed to initialize."
            
        try:
            result = await self.rag_chain.ainvoke({"input": query})
            return result.get("answer", "No answer found.")
        except Exception as e:
            logger.error(f"RAG query failed: {e}")
            return f"Error querying knowledge base: {e}"

rag_service = RAGService()

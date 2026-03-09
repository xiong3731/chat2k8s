import sys
import os
import pickle
import uuid
import logging
from typing import List, Dict, Any, Optional

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_milvus import Milvus, BM25BuiltInFunction
from langchain_classic.storage import InMemoryStore

from app.core.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def initialize_vector_db(force_recreate: bool = False):
    """Initializes the vector DB: loads docs, splits, ingests to Milvus, saves parent store."""
    
    # Initialize Embeddings (copied from RAGService)
    embeddings = OpenAIEmbeddings(
        model=settings.RAG_EMBEDDING_MODEL,
        openai_api_key=settings.RAG_EMBEDDING_API_KEY,
        openai_api_base=settings.RAG_EMBEDDING_API_BASE
    )
    
    store_path = "./doc_path/parent_store.pkl"
    
    # Ensure doc_path exists
    if not os.path.exists('./doc_path/rag_doc'):
        os.makedirs('./doc_path/rag_doc')
        
    logger.info("Loading documents from ./doc_path/rag_doc/...")
    raw_docs = DirectoryLoader('./doc_path/rag_doc/', glob="**/*.md", loader_cls=TextLoader, loader_kwargs={'encoding': 'utf-8'}).load()
    
    if not raw_docs:
        logger.warning("No markdown documents found in ./doc_path/rag_doc/. RAG service initialized without documents.")
        return

    md_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[("#", "H1"), ("##", "H2"), ("###", "H3")])

    md_docs = []
    for doc in raw_docs:
        splits = md_splitter.split_text(doc.page_content)
        for split in splits:
            split.metadata.update(doc.metadata)
            md_docs.append(split)

    parent_splitter = RecursiveCharacterTextSplitter(chunk_size=4096, chunk_overlap=200)
    child_splitter = RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=150)

    all_child_docs = []
    
    # Clear existing store
    store = InMemoryStore()

    logger.info("Splitting documents...")
    for parent_doc in parent_splitter.split_documents(md_docs):
        pid = str(uuid.uuid4())
        store.mset([(pid, parent_doc)])
        for child in child_splitter.split_documents([parent_doc]):
            child.metadata["parent_doc_id"] = pid
            all_child_docs.append(child)

    logger.info(f"Building vector store with {len(all_child_docs)} child documents...")
    
    # Normalize metadata to ensure schema consistency
    all_keys = set()
    for doc in all_child_docs:
        all_keys.update(doc.metadata.keys())
        
    for doc in all_child_docs:
        for key in all_keys:
            if key not in doc.metadata:
                doc.metadata[key] = ""
    
    # Build Vector Store
    # Note: drop_old=True will clear existing collection
    Milvus.from_documents(
        documents=all_child_docs,
        embedding=embeddings,
        builtin_function=BM25BuiltInFunction(output_field_names="sparse_vector"),
        connection_args={"host": settings.RAG_MILVUS_HOST, "port": settings.RAG_MILVUS_PORT},
        collection_name=settings.RAG_MILVUS_COLLECTION_NAME,
        drop_old=True,
        vector_field=["dense_vector", "sparse_vector"]
    )
    
    # Save parent store
    logger.info(f"Saving parent store to {store_path}...")
    try:
        # We need to access the underlying storage of InMemoryStore to pickle it
        store_data = {}
        for key in store.yield_keys():
            val = store.mget([key])[0]
            if val:
                store_data[key] = val
        
        with open(store_path, 'wb') as f:
            pickle.dump(store_data, f)
        logger.info("Parent store saved successfully.")
    except Exception as e:
        logger.error(f"Failed to save parent store: {e}")

if __name__ == "__main__":
    logging.info("Starting Vector Database Initialization...")
    try:
        initialize_vector_db(force_recreate=True)
        logging.info("Vector Database Initialized Successfully.")
    except Exception as e:
        logging.error(f"Failed to initialize Vector Database: {e}")
        sys.exit(1)

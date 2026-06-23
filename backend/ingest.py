from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from fastembed import TextEmbedding
import uuid

# 1. Connect to our local Qdrant Docker container
client = QdrantClient(url="http://localhost:6333")

# 2. Initialize the local CPU embedding model
print("Downloading/Loading local embedding model...")
embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

COLLECTION_NAME = "corporate_memory"

# 3. Create a fresh vector collection
if client.collection_exists(collection_name=COLLECTION_NAME):
    client.delete_collection(collection_name=COLLECTION_NAME)

client.create_collection(
    collection_name=COLLECTION_NAME,
    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
)

# 4. Read the internal document
with open("../data_vault/company_handbook.txt", "r") as file:
    corporate_text = file.read()

# 5. Chunk and Embed the data
print("Embedding corporate data...")
# (In a production app, you would chunk this into smaller 500-word blocks)
chunks = [corporate_text]
embeddings = list(embedding_model.embed(chunks))

# 6. Upload to Qdrant
points = [
    PointStruct(
        id=str(uuid.uuid4()), 
        vector=embeddings[0].tolist(), 
        payload={"text": chunks[0], "source": "company_handbook.txt"}
    )
]

client.upsert(collection_name=COLLECTION_NAME, points=points)
print("✅ Corporate memory successfully injected into Qdrant Vault!")

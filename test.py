from neo4j import GraphDatabase

driver = GraphDatabase.driver(
    "bolt://localhost:7687",
    auth=("neo4j","tfyfu8ub")
)

with driver.session() as session:

    result = session.run("""

        CREATE (:User {name:"Akshay"})

    """)

print("Done")